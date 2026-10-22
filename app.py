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
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import date, timedelta, datetime, timezone
from functools import wraps

import bcrypt
from flask import (
    Flask, Response, flash, g, jsonify, make_response, redirect,
    render_template, request, session, url_for,
)
from zoneinfo import ZoneInfo

def _asset_version():
    """Hash of CODE files only — data pushes (tickets, plays.json) must NOT
    trigger the 'new version' banner. Only real code changes do."""
    try:
        import hashlib
        base = os.path.dirname(os.path.abspath(__file__))
        h = hashlib.md5()
        # Code paths that constitute a "new version" of the page
        code_paths = [
            os.path.join(base, "app.py"),
            os.path.join(base, "templates"),
            os.path.join(base, "static", "css"),
            os.path.join(base, "static", "js"),
        ]
        for p in code_paths:
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    h.update(f.read())
            elif os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    for fn in sorted(files):
                        fp = os.path.join(root, fn)
                        with open(fp, "rb") as f:
                            h.update(f.read())
        return h.hexdigest()[:10]
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

# Link de PAGO de Stripe para Platinum (orden de Alex 2026-10-19): $1 la primera
# semana (7 días de trial dentro del link) y luego $15.99/semana. El mes gratis
# de plataforma MURIÓ: ya no se otorga platinum_trial_ends al registrarse ni al
# activar la cuenta; los relojes ya vigentes corren hasta su fecha sin tocarse.
# Link: plink_1USPKERwCjFIsFzUtk6yQ6kg = one-time $1 (price_1USPJnRwCjFIsFzU4pYcIqnq)
# + recurrente $15.99/sem (price_1USPJvRwCjFIsFzUH8rwj5K8), trial_period_days=7.
# Se cambia sin tocar código con la env var STRIPE_PLATINUM_URL en Render.
STRIPE_PLATINUM_URL = os.environ.get(
    "STRIPE_PLATINUM_URL", "https://buy.stripe.com/5kQdRbfFmgHideubEQefC07"
)
# Pedido Alex 2026-10-05: checkout de Stripe siempre en inglés. Stripe
# localiza la página de pago según el navegador del cliente (si está en
# español, todo sale en español); el parámetro locale=en lo fija en inglés.
if "locale=" not in STRIPE_PLATINUM_URL:
    _sep = "&" if "?" in STRIPE_PLATINUM_URL else "?"
    STRIPE_PLATINUM_URL = f"{STRIPE_PLATINUM_URL}{_sep}locale=en"

# Tracker "Gold" (solo Alex): stake fijo por jugada para validar el
# sistema en 200 jugadas antes del lanzamiento público. Se cambia con la
# env var FREE_STAKE en Render (default $50).
FREE_STAKE = float(os.environ.get("FREE_STAKE", "50"))
# Capital simulado del tracker de validación "Gold" (2026-10-03, orden
# de Alex): $10,000 al 1% estilo WGT — VIP 1u = $100, Free 0.6u = $60.
FREE_TEST_BANKROLL = float(os.environ.get("FREE_TEST_BANKROLL", "10000"))
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


# Publishable key (pk_live) para Stripe.js en el checkout incrustado de
# /desbloquear-platinum. Es pública por diseño, pero la API de Stripe NO la
# devuelve: sale del Dashboard de Stripe (Developers -> API keys). Se lee de
# la env var STRIPE_PUBLISHABLE_KEY; si está vacía, se usa como respaldo la
# constante de abajo (la pk_live pública de la cuenta, entregada por Alex el
# 2026-10-19) y la env var siempre manda cuando existe. Sin llave, la página
# cae al link hospedado.
STRIPE_PUBLISHABLE_KEY = os.environ.get(
    "STRIPE_PUBLISHABLE_KEY", ""
) or "pk_live_51UIyagRwCjFIsFzUW89rnPLseuYlxaUMrRIe2bIpPteTHAHefiUbYTrXE9rTDsGGDGrD3c7SofspGpmE4jcRdHEL00vCim5NrT"
# Precios de la oferta vigente — los mismos del Payment Link hospedado:
# $1 la primera semana (one-time) + $15.99/semana (recurrente).
PLATINUM_PRICE_PRIMERA_SEMANA = "price_1USPJnRwCjFIsFzU4pYcIqnq"
PLATINUM_PRICE_SEMANAL = "price_1USPJvRwCjFIsFzUH8rwj5K8"


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
# Token del bot para avisos en tiempo real (ruta /go/<slug>).
# Se configura en Render como env var TELEGRAM_BOT_TOKEN. Si no está,
# el aviso lo sigue mandando el watcher de la VM (cron click-notify).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# Chat de Alex para los avisos de clics (su telegram_user_id).
CLICK_NOTIFY_CHAT_ID = os.environ.get("CLICK_NOTIFY_CHAT_ID", "8600523481").strip()
# Link permanente de invitación al canal privado Platinum (Telegram).
# 2026-10-02: reemplaza el pool de links de un solo uso (fricción: Telegram
# muestra "Expired Link" a usuarios baneados y los links se agotaban).
# Es directo, sin expiración ni límite de miembros; el control de acceso lo
# hace el ban-sync (los que cancelan son baneados del canal en ~10 min y un
# baneado no puede entrar aunque tenga el link).
TELEGRAM_VIP_INVITE_LINK = "https://t.me/+_eNhcbD3k4RlNTk5"

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


def _edge_num(v):
    """Edge como número para la BD (fix 2026-10-12): plays.json trae el edge
    como texto ('+5.7%') y la columna edge de Postgres es DOUBLE PRECISION —
    insertar el texto explotaba con HTTP 500 y NINGÚN miembro podía trackear
    (en SQLite no fallaba por la afinidad de tipos). Devuelve float o None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("%", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None
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
PENDING_PLAYS_PATH = os.path.join(BASE_DIR, "data", "plays-pending.json")
# Lectura fresca de datos (2026-10-15, pedido por Alex: que las jugadas
# caigan al Dashboard en el menor tiempo posible tras su disparo): las
# lecturas de plays/pendings van primero al raw de GitHub master — el dato
# se publica por commit y así NO hay que esperar el redeploy de Render.
PLAYS_RAW_URL = ("https://raw.githubusercontent.com/penamanuel-art/"
                 "tlb-members/master/data/plays.json")
PENDING_RAW_URL = ("https://raw.githubusercontent.com/penamanuel-art/"
                   "tlb-members/master/data/plays-pending.json")
_REMOTE_JSON_CACHE = {}
# Cuenta personal de miembro de Alex (NO es la de administrador). Es la única
# que ve la card pendiente (sin tickets) en /home — orden de Alex 2026-10-03.
ALEX_MEMBER_EMAIL = "pena.manuel@myyahoo.com"
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
    notif_plays INTEGER NOT NULL DEFAULT 1,     -- 1 = avisar jugadas publicadas
    notif_graded INTEGER NOT NULL DEFAULT 1,    -- 1 = avisar W/L al liquidarse
    notif_live INTEGER NOT NULL DEFAULT 1,      -- 1 = marcador en vivo (lock screen) de las jugadas oficiales
    notif_reminder INTEGER NOT NULL DEFAULT 1,  -- 1 = recordatorio 15 min antes
    notif_ev INTEGER NOT NULL DEFAULT 1,        -- 1 = alertas +EV
    notif_price INTEGER NOT NULL DEFAULT 0,    -- 1 = alerta si se mueve el precio
    notif_sharp INTEGER NOT NULL DEFAULT 1,     -- 1 = alertas sharp money / line movement (2026-10-19)
    notif_masterclass INTEGER NOT NULL DEFAULT 1, -- 1 = aviso de la leccion diaria Masterclass (2026-10-19)
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
    juego TEXT,              -- ej. "Tulsa vs Navy" (para lookup de scores)
    liga TEXT,               -- clave de deporte the-odds-api (ej. americanfootball_ncaaf)
    created_at TEXT NOT NULL,
    UNIQUE(user_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_plays(user_id);
-- Tracker paralelo "Gold" (solo Alex): las jugadas recomendadas se
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
-- (book_balances se crea por migración separada, no en el DDL inicial)
CREATE INDEX IF NOT EXISTS idx_push_member ON push_subscriptions(member_id);
CREATE TABLE IF NOT EXISTS stripe_events (   -- auditoría de webhooks de Stripe (deduplicada por event_id)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    email TEXT,
    detalle TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notificaciones (  -- centro de notificaciones del admin (2026-10-07)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo TEXT NOT NULL,        -- 'miembro_nuevo', 'ticket_subido', 'platinum', 'cancelacion', 'pago'
    titulo TEXT NOT NULL,
    detalle TEXT,
    created_at TEXT NOT NULL,
    leida INTEGER NOT NULL DEFAULT 0
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
    notif_plays INTEGER NOT NULL DEFAULT 1,     -- 1 = avisar jugadas publicadas
    notif_graded INTEGER NOT NULL DEFAULT 1,    -- 1 = avisar W/L al liquidarse
    notif_live INTEGER NOT NULL DEFAULT 1,      -- 1 = marcador en vivo (lock screen) de las jugadas oficiales
    notif_reminder INTEGER NOT NULL DEFAULT 1,  -- 1 = recordatorio 15 min antes
    notif_ev INTEGER NOT NULL DEFAULT 1,        -- 1 = alertas +EV
    notif_price INTEGER NOT NULL DEFAULT 0,    -- 1 = alerta si se mueve el precio
    notif_sharp INTEGER NOT NULL DEFAULT 1,     -- 1 = alertas sharp money / line movement (2026-10-19)
    notif_masterclass INTEGER NOT NULL DEFAULT 1, -- 1 = aviso de la leccion diaria Masterclass (2026-10-19)
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
    juego TEXT,              -- ej. "Tulsa vs Navy" (para lookup de scores)
    liga TEXT,               -- clave de deporte the-odds-api (ej. americanfootball_ncaaf)
    created_at TEXT NOT NULL,
    UNIQUE(user_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_plays(user_id);
-- Tracker paralelo "Gold" (solo Alex): las jugadas recomendadas se
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
-- (book_balances se crea por migración separada, no en el DDL inicial)
CREATE TABLE IF NOT EXISTS stripe_events (   -- auditoría de webhooks de Stripe (deduplicada por event_id)
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    email TEXT,
    detalle TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notificaciones (  -- centro de notificaciones del admin (2026-10-07)
    id SERIAL PRIMARY KEY,
    tipo TEXT NOT NULL,
    titulo TEXT NOT NULL,
    detalle TEXT,
    created_at TEXT NOT NULL,
    leida INTEGER NOT NULL DEFAULT 0
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


def fecha_corta() -> str:
    """Fecha compacta para el hero (una línea en móvil)."""
    d = datetime.now(TZ).date()
    return f"{DAYS_EN[d.weekday()][:3]}, {MONTHS_EN[d.month - 1][:3]} {d.day}, {d.year}"


def fecha_larga() -> str:
    """Fecha larga estilo WGT para el hero (ej. 'Sunday, October 4')."""
    d = datetime.now(TZ).date()
    return f"{DAYS_EN[d.weekday()]}, {MONTHS_EN[d.month - 1]} {d.day}"


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
            "INSERT INTO checkins (user_id, fecha) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM users WHERE id = ?) ON CONFLICT DO NOTHING",
            (user_id, today_iso(), user_id),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO checkins (user_id, fecha) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM users WHERE id = ?)",
            (user_id, today_iso(), user_id),
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
        if current_user() is None:
            # Sesion fantasma: la cuenta ya no existe (p. ej. eliminada en una
            # limpieza). Sin esto, vistas como /home reventaban con 500 al
            # registrar el checkin (FK violation) — bug real 2026-10-21.
            session.clear()
            flash("Log in to continue.", "warn")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _admin_o_cuenta_alex(user) -> bool:
    """Admin real O la cuenta de miembro de Alex (2026-10-17, pedido suyo:
    entrar al Admin Dashboard sin salir de su cuenta de miembro)."""
    if is_admin_for(user):
        return True
    try:
        return bool(user) and (user["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL.strip().lower()
    except Exception:
        return False


def admin_required(view):
    """Solo administradores: requiere login + flag de admin."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not _admin_o_cuenta_alex(current_user()):
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if row is not None and _apagar_trial_si_vencido(db, row):
        row = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return row


def _trial_vencido(user) -> bool:
    """True si el mes gratis de plataforma (reloj interno, sin Stripe)
    ya venció. Quien paga por Stripe no lleva reloj: manda su sub."""
    try:
        te = user["platinum_trial_ends"]
        sub = user["stripe_subscription_id"]
    except (KeyError, IndexError, TypeError):
        return False
    if not te or sub:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(te)
    except (ValueError, TypeError):
        return False


def _apagar_trial_si_vencido(db, user) -> bool:
    """Apaga el Platinum de UN usuario si su mes gratis venció."""
    try:
        if user and user["platinum_unlocked"] and _trial_vencido(user):
            db.execute(
                "UPDATE users SET platinum_unlocked = 0, platinum_trial_ends = NULL WHERE id = ?",
                (user["id"],),
            )
            db.commit()
            return True
    except (KeyError, IndexError, TypeError):
        pass
    return False


def _barrer_trials_vencidos(db):
    """Apaga todos los Platinum cuyo mes gratis de plataforma venció
    (sin suscripción de Stripe). Se llama al abrir el panel de miembros
    y la API de miembros: lo que Alex ve siempre es la verdad."""
    db.execute(
        "UPDATE users SET platinum_unlocked = 0, platinum_trial_ends = NULL "
        "WHERE platinum_unlocked = 1 AND platinum_trial_ends IS NOT NULL "
        "AND platinum_trial_ends < ? "
        "AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')",
        (datetime.now().isoformat(),),
    )
    db.commit()


def platinum_unlocked_for(user) -> bool:
    """True si el miembro desbloqueó la jugada Elite (default: bloqueada).
    El mes gratis de plataforma vence solo: con el reloj pasado y sin
    suscripción de Stripe, el acceso cuenta como apagado."""
    try:
        return bool(user and user["platinum_unlocked"]) and not _trial_vencido(user)
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
                 "Content-Type": "application/json",
                 # Resend va detrás de Cloudflare: sin UA de navegador
                 # devuelve 403 (error 1010, browser integrity check).
                 "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 "
                               "like Mac OS X) AppleWebKit/605.1.15"),
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(2000).decode("utf-8", "replace")
            ok = 200 <= r.status < 300
            return ok, f"resend HTTP {r.status}: {body[:200]}"
    except Exception as e:
        extra = ""
        try:  # HTTPError trae el cuerpo con el motivo real
            code = getattr(e, "code", "?")
            extra = f" [HTTP {code}] " + e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return False, f"resend falló: {type(e).__name__}: {e}{extra}".strip()


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


# ---------- Activación por correo + captcha anti-robots (2026-10-18) ----------
# Orden de Alex tras detectar registros basura de Meta: la cuenta nueva nace
# SIN activar y solo se activa cuando el dueño toca el enlace que llega a su
# correo (un correo inventado nunca se activa). La bienvenida arranca AL
# ACTIVAR, no al llenar el formulario (el mes gratis de Platinum al activar
# murió el 2026-10-19 — ver activar_cuenta). El captcha
# de Cloudflare Turnstile frena los registros automáticos; sin llaves en el
# entorno, el captcha queda dormido y el registro sigue funcionando.
TURNSTILE_SITE_KEY = (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()
TURNSTILE_SECRET_KEY = (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()
SITE_URL = "https://www.thesharpteam.bet"


def _turnstile_ok() -> bool:
    """Verifica el captcha Turnstile del formulario. Sin llave secreta
    configurada devuelve True (no bloquea). Ante error de red: False."""
    if not TURNSTILE_SECRET_KEY:
        return True
    token = (request.form.get("cf-turnstile-response") or "").strip()
    if not token:
        return False
    try:
        from urllib.parse import urlencode
        data = urlencode({"secret": TURNSTILE_SECRET_KEY,
                          "response": token}).encode()
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data)
        with urllib.request.urlopen(req, timeout=8) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("success"))
    except Exception:
        return False


_ACTIVATION_FOOTER = """<tr><td style="background:#ffffff;border-top:1px solid #e2e8f0;border-radius:0 0 16px 16px;padding:22px 24px 20px;text-align:center;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" width="76" alt="The Sharp Team" style="display:block;margin:0 auto 8px;">
<div style="font-size:15px;font-weight:800;color:#0f172a;letter-spacing:1.5px;">THE SHARP TEAM</div>
<div style="font-size:12.5px;color:#64748b;margin-top:5px;">250 Park Avenue, Suite 1800, New York, NY 10017 &middot; (551) 326-3312</div>
<div style="margin-top:12px;">
<a href="https://www.instagram.com/the_sharp_team" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/instagram.png" width="30" height="30" alt="Instagram" style="border:0;margin:0 6px;"></a>
<a href="https://x.com/thesharpteambet" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/x.png" width="30" height="30" alt="X" style="border:0;margin:0 6px;"></a>
<a href="https://t.me/thesharpteam" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/telegram.png" width="30" height="30" alt="Telegram" style="border:0;margin:0 6px;"></a>
</div>
<div style="font-size:11.5px;color:#64748b;line-height:1.5;margin-top:12px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text 1-800-GAMBLER.</div>
<div style="font-size:11.5px;color:#64748b;margin-top:8px;">Don't want these emails anymore? <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#64748b;text-decoration:underline;">Unsubscribe</a> — one click, no questions asked.</div>
<div style="font-size:11.5px;margin-top:8px;"><a href="https://www.thesharpteam.bet/terms" style="color:#64748b;">Terms</a> &middot; <a href="https://www.thesharpteam.bet/privacy" style="color:#64748b;">Privacy Policy</a></div>
<div style="font-size:11px;color:#94a3b8;margin-top:6px;">&copy; 2026 The Sharp Team. All rights reserved.</div>
</td></tr>"""


def _activation_shell(titulo, cuerpo, boton, nota, link, lang="en"):
    """Carcasa de los emails de activación — Diseño D (2026-10-22,
    aprobado por Alex: fondo claro, cero negro, línea verde 3D y el pie
    universal claro con iconos). La usan la activación y su
    recordatorio; los textos llegan de cada función."""
    if lang == "es":
        eyebrow = "UN ÚLTIMO PASO"
        checks = ("Tus jugadas Gold se activan de inmediato",
                  "Tu Dashboard se abre en cuanto activas tu cuenta",
                  "Platinum empieza por $1 cuando quieras")
        o_abre = "O abre este enlace:"
    else:
        eyebrow = "ONE LAST STEP"
        checks = ("Your Gold plays go live right away",
                  "Your Dashboard opens the moment you activate",
                  "Platinum starts for $1 whenever you're ready")
        o_abre = "Or open this link:"
    checks_html = "".join(
        f'<tr><td style="font-size:14.5px;color:#065f46;padding:3px 0;">&#10003;&nbsp; {c}</td></tr>'
        for c in checks)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%;text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
<tr><td align="center" style="padding:22px 12px 30px;">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;">
  <tr><td style="background:#ffffff;border-radius:16px 16px 0 0;padding:24px 28px 0;text-align:center;">
    <img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" width="92" alt="The Sharp Team" style="display:block;margin:0 auto;border:0;">
    <div style="font-size:13.5px;font-weight:800;color:#0f172a;letter-spacing:3px;margin-top:10px;">THE SHARP TEAM</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0 0;"><tr><td bgcolor="#9ff7d4" height="3" style="height:3px;font-size:0;line-height:0;border-top-left-radius:3px;border-top-right-radius:3px;">&nbsp;</td></tr><tr><td bgcolor="#2ee6a8" height="6" style="height:6px;font-size:0;line-height:0;">&nbsp;</td></tr><tr><td bgcolor="#0b8a5f" height="3" style="height:3px;font-size:0;line-height:0;border-bottom-left-radius:3px;border-bottom-right-radius:3px;">&nbsp;</td></tr></table>
  </td></tr>
  <tr><td style="background:#ffffff;padding:22px 28px 6px;text-align:center;">
    <div style="color:#b45309;font-size:11.5px;font-weight:800;letter-spacing:2px;">{eyebrow}</div>
    <div style="font-size:25px;font-weight:900;color:#0f172a;margin-top:8px;">{titulo}</div>
    <p style="font-size:15px;line-height:1.6;color:#334155;margin:14px 0 0;">{cuerpo}</p>
    <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:18px auto 2px;text-align:left;">
      {checks_html}
    </table>
    <div style="padding:20px 0 4px;">
      <a href="{link}" style="display:inline-block;background:#059669;color:#ffffff;font-size:15px;font-weight:800;padding:14px 36px;border-radius:999px;text-decoration:none;">{boton}</a>
    </div>
    <p style="font-size:12px;color:#64748b;margin:12px 0 0;">{o_abre}<br><a href="{link}" style="color:#2563eb;text-decoration:underline;">{link}</a></p>
    <p style="font-size:11.5px;color:#94a3b8;line-height:1.5;margin:14px 0 0;">{nota}</p>
  </td></tr>
  {_ACTIVATION_FOOTER}
</table>
</td></tr></table>
</body></html>"""


def send_activation_email(nombre: str, email: str, token: str, lang: str = "en"):
    """Email de activación de cuenta (hilo aparte, nunca rompe el registro).

    El enlace apunta a /activar/<token>. EN por defecto; ES si lang == 'es'.
    """
    link = f"{SITE_URL}/activar/{token}"
    if lang == "es":
        subject = "Activa tu cuenta de The Sharp Team"
        titulo = "Activa tu cuenta"
        cuerpo = (f"Hola {nombre} — toca el botón para activar tu cuenta de "
                  "The Sharp Team. Tus jugadas Gold y tu Dashboard quedan "
                  "activos en el momento en que activas — y Platinum "
                  "empieza por $1 cuando quieras.")
        boton = "ACTIVAR MI CUENTA"
        nota = ("¿No se creó esta cuenta con tu correo? Ignora este mensaje "
                "y la cuenta nunca se activará.")
        texto = (f"Hola {nombre},\n\nActiva tu cuenta de The Sharp Team "
                 f"tocando este enlace:\n{link}\n\nTus jugadas Gold quedan "
                 "activas al activar, y Platinum empieza por $1 cuando "
                 "quieras. Si no creaste esta cuenta, ignora "
                 "este mensaje.\n\nThe Sharp Team")
    else:
        subject = "Activate your The Sharp Team account"
        titulo = "Activate your account"
        cuerpo = (f"Hi {nombre} — tap the button to activate your "
                  "The Sharp Team account. Your Gold plays and your "
                  "Dashboard go live the moment you activate — and "
                  "Platinum starts for $1 whenever you're ready.")
        boton = "ACTIVATE MY ACCOUNT"
        nota = ("Didn't create this account? Ignore this email and the "
                "account will never be activated.")
        texto = (f"Hi {nombre},\n\nActivate your The Sharp Team account by "
                 f"opening this link:\n{link}\n\nYour Gold plays go live "
                 "when you activate, and Platinum starts for $1 whenever "
                 "you're ready. If you didn't create this "
                 "account, ignore this email.\n\nThe Sharp Team")
    html = _activation_shell(titulo, cuerpo, boton, nota, link, lang)

    def _send():
        try:
            _dispatch_email(email, subject, texto, html)
        except Exception:
            pass  # silencioso: nunca rompe el registro

    import threading
    threading.Thread(target=_send, daemon=True).start()


def send_activation_reminder_email(nombre: str, email: str, token: str, lang: str = "en") -> tuple:
    """Recordatorio de activación (2026-10-19, orden de Alex): quien se
    registró pero no confirmó su cuenta recibe un aviso de que debe
    confirmar para activarla (desde el 2026-10-19 activar ya no otorga
    Platinum: el aviso dice que sus Gold quedan activas y que Platinum
    empieza por $1).
    Síncrono (devuelve el resultado real) porque lo dispara un endpoint
    que debe contar solo los envíos que sí salieron."""
    link = f"{SITE_URL}/activar/{token}"
    if lang == "es":
        subject = "Recordatorio: confirma tu cuenta de The Sharp Team"
        titulo = "Tu cuenta aún no está activa"
        cuerpo = (f"Hola {nombre} — creaste tu cuenta de The Sharp Team pero "
                  "todavía no la has confirmado. Toca el botón para confirmar "
                  "tu correo y activar tu cuenta: tus jugadas Gold quedan "
                  "activas en ese momento, y Platinum empieza por $1.")
        boton = "CONFIRMAR MI CUENTA"
        nota = ("¿No se creó esta cuenta con tu correo? Ignora este mensaje "
                "y la cuenta nunca se activará.")
        texto = (f"Hola {nombre},\n\nCreaste tu cuenta de The Sharp Team pero "
                 f"todavía no la has confirmado. Confírmala aquí:\n{link}\n\n"
                 "Tus jugadas Gold quedan activas al confirmar, y Platinum "
                 "empieza por $1 cuando quieras. Si no "
                 "creaste esta cuenta, ignora este mensaje.\n\nThe Sharp Team")
    else:
        subject = "Reminder: confirm your The Sharp Team account"
        titulo = "Your account is not active yet"
        cuerpo = (f"Hi {nombre} — you created your The Sharp Team account but "
                  "haven't confirmed it yet. Tap the button to confirm your "
                  "email and activate your account: your Gold plays go "
                  "live the moment you do, and Platinum starts for $1.")
        boton = "CONFIRM MY ACCOUNT"
        nota = ("Didn't create this account? Ignore this email and the "
                "account will never be activated.")
        texto = (f"Hi {nombre},\n\nYou created your The Sharp Team account but "
                 f"haven't confirmed it yet. Confirm it here:\n{link}\n\n"
                 "Your Gold plays go live when you confirm, and Platinum "
                 "starts for $1 whenever you're ready. If you "
                 "didn't create this account, ignore this email.\n\nThe Sharp Team")
    html = _activation_shell(titulo, cuerpo, boton, nota, link, lang)
    return _dispatch_email(email, subject, texto, html)


@app.route("/api/activation-reminders", methods=["POST"])
def api_activation_reminders():
    """Dispara los recordatorios de activación (2026-10-19, orden de Alex).

    A quien se registró pero NO confirmó su cuenta se le recuerda a las
    ~3 horas del registro y otra vez a las ~24 horas (máximo 2). Sin
    confirmar no es miembro: estos correos son el único contacto.
    Protegido con header X-Push-Key == PUSH_TRIGGER_KEY.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    db = get_db()
    ahora = datetime.now(timezone.utc)
    rows = db.execute(
        "SELECT id, nombre, email, lang, created_at, activation_token, activation_reminders "
        "FROM users WHERE email_verified = 0 AND activation_token IS NOT NULL "
        "AND is_admin = 0 AND activation_reminders < 2 "
        "AND LOWER(email) != 'system@value.local' "
        "ORDER BY created_at ASC LIMIT 20"
    ).fetchall()
    enviados = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        try:
            creado = datetime.fromisoformat(d["created_at"])
            if creado.tzinfo is None:
                creado = creado.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        edad_h = (ahora - creado).total_seconds() / 3600.0
        n = d.get("activation_reminders") or 0
        if n == 0 and edad_h < 3:
            continue
        if n >= 1 and edad_h < 24:
            continue
        try:
            ok, _det = send_activation_reminder_email(
                d.get("nombre") or "there", d["email"],
                d["activation_token"], d.get("lang") or "en")
        except Exception:
            ok = False
        if ok:
            db.execute(
                "UPDATE users SET activation_reminders = ?, activation_reminder_last = ? WHERE id = ?",
                (n + 1, ahora.isoformat(), d["id"]))
            db.commit()
            enviados.append({"nombre": d.get("nombre"), "email": d.get("email"),
                             "recordatorio": n + 1})
    return jsonify({"ok": True, "reminders_sent": enviados})


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


# ----------------- Welcome back (re-suscripción, pedido Alex 2026-10-08) ----
WELCOME_BACK_SUBJECT = "Welcome back. You're back on track."

WELCOME_BACK_TEXT = """Hi __NOMBRE__,

Welcome back to The Sharp Team. Your Platinum access is live again, right where you left it.

TODAY'S CARD IS WAITING - Today's official plays (Gold plus the Platinum) are live on your dashboard now, each with its original bet ticket.

YOUR TRACKER IS INTACT - Every play you tracked is still there. Record, ROI and streaks picked up right where they stopped.

EVERYTHING RESUMES INSTANTLY - Push alerts, Telegram channels, the +EV board and the Daily Academy. All back on, no setup needed.

"Every play is posted before game time - then locked and graded against the final score."

Enter your dashboard:
https://www.thesharpteam.bet/home

$1 for the first 7 days, then $15.99/week. Cancel anytime.

- The Sharp Team

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

WELCOME_BACK_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>Welcome back to The Sharp Team</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">Welcome back to The Sharp Team. Your Platinum plays are live again.</div>
<div style="font-family:Arial,sans-serif;max-width:460px;margin:0 auto;background:#ffffff;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-banner.png" alt="The Sharp Team" width="460" style="width:100%;height:auto;display:block;border:0;">
<div style="background:#ffffff;padding:14px 0 12px;text-align:center;">
<p style="margin:0;font-size:13px;font-weight:800;letter-spacing:5px;color:#2e4053;">THE SHARP TEAM</p>
</div>
<div style="background:#c9a227;padding:28px 24px;text-align:center;">
<p style="margin:0;font-size:12px;font-weight:800;letter-spacing:3px;color:#0a0f1c;">WELCOME BACK</p>
<h1 style="font-size:30px;font-weight:800;color:#0a0f1c;margin:10px 0 0;line-height:1.25;">Back on track.</h1>
</div>
<div style="text-align:center;padding:16px 20px 0;">
<p style="color:#555555;font-size:15px;line-height:1.6;margin:0;">Hi __NOMBRE__ &mdash; good to have you back. Your Platinum access is live again, right where you left it:</p>
</div>
<div style="padding:14px 20px 0;">
<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 10px;background:#2e4053;border:1px solid #2e4053;border-radius:12px;"><tr>
<td width="44" style="vertical-align:top;padding:14px 0 14px 16px;"><div style="font-size:24px;line-height:1.2;">🎯</div></td>
<td style="vertical-align:top;padding:14px 16px 14px 4px;"><div style="font-size:14px;font-weight:800;color:#ffffff;letter-spacing:1px;">TODAY&rsquo;S CARD IS WAITING</div><div style="font-size:13px;color:#c8d0dc;line-height:1.6;margin-top:4px;">Today&rsquo;s official plays &mdash; Gold plus the Platinum &mdash; are live on your dashboard now, each with its original ticket.</div></td>
</tr></table>
<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 10px;background:#2e4053;border:1px solid #2e4053;border-radius:12px;"><tr>
<td width="44" style="vertical-align:top;padding:14px 0 14px 16px;"><div style="font-size:24px;line-height:1.2;">📊</div></td>
<td style="vertical-align:top;padding:14px 16px 14px 4px;"><div style="font-size:14px;font-weight:800;color:#ffffff;letter-spacing:1px;">YOUR TRACKER IS INTACT</div><div style="font-size:13px;color:#c8d0dc;line-height:1.6;margin-top:4px;">Every play you tracked is still there &mdash; record, ROI and streaks picked up right where they stopped.</div></td>
</tr></table>
<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 10px;background:#2e4053;border:1px solid #2e4053;border-radius:12px;"><tr>
<td width="44" style="vertical-align:top;padding:14px 0 14px 16px;"><div style="font-size:24px;line-height:1.2;">⚡</div></td>
<td style="vertical-align:top;padding:14px 16px 14px 4px;"><div style="font-size:14px;font-weight:800;color:#ffffff;letter-spacing:1px;">EVERYTHING RESUMES INSTANTLY</div><div style="font-size:13px;color:#c8d0dc;line-height:1.6;margin-top:4px;">Push alerts, Telegram channels, the +EV board and the Daily Academy &mdash; all back on, no setup needed.</div></td>
</tr></table>
</div>
<div style="margin:14px 20px 0;padding:14px 18px;border-left:3px solid #22c55e;">
<p style="font-size:15px;font-style:italic;line-height:1.7;color:#555555;margin:0;">&ldquo;Every play is posted before game time &mdash; then locked and graded against the final score.&rdquo;</p>
</div>
<div style="text-align:center;padding:18px 20px 0;">
<a href="https://www.thesharpteam.bet/home" style="display:inline-block;background:#22c55e;color:#111111;font-size:14px;font-weight:800;padding:11px 28px;border-radius:999px;text-decoration:none;">ENTER THE DASHBOARD</a>
<p style="color:#555555;font-size:14px;line-height:1.7;margin:14px 0 0;">$1 for the first 7 days &mdash; then $15.99/week.<br>Cancel anytime.</p>
</div>
<div style="text-align:center;padding:20px 20px 14px;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" alt="The Sharp Team" width="110" style="width:110px;height:auto;display:block;margin:0 auto;border:0;">
<p style="margin:10px 0 0;color:#111111;font-size:12px;font-weight:800;letter-spacing:3px;">THE SHARP TEAM</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:11px;">250 Park Avenue, Suite 1800, New York, NY 10017</p>
<p style="margin:4px 0 0;color:#9ca3af;font-size:11px;"><a href="tel:+15513263312" style="color:#9ca3af;text-decoration:none;">(551) 326-3312</a></p>
<p style="margin:12px 0 0;color:#9ca3af;font-size:10px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text <a href="tel:18004262537" style="color:#9ca3af;text-decoration:underline;">1-800-GAMBLER</a>.</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:10px;">Don't want these emails anymore? <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#9ca3af;text-decoration:underline;">Unsubscribe</a> — one click, no questions asked.</p>
<p style="margin:6px 0 0;color:#9ca3af;font-size:10px;">&copy; 2026 The Sharp Team</p>
</div>
</div>
</div>
</body>
</html>
"""


def send_welcome_back_email(nombre: str, email: str):
    """Email de bienvenida para miembros que se RE-suscriben (pedido Alex 2026-10-08).
    Tono distinto al de primera vez: más corto, "back on track"."""
    resend_ok = bool((os.environ.get("RESEND_API_KEY") or "").strip())
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not (resend_ok or (user and pwd)):
        return

    subject = WELCOME_BACK_SUBJECT
    text = WELCOME_BACK_TEXT.replace("__NOMBRE__", nombre)
    html = WELCOME_BACK_HTML.replace("__NOMBRE__", nombre)

    def _send():
        try:
            _dispatch_email(email, subject, text, html)
        except Exception:
            pass  # silencioso: nunca rompe la activación

    import threading
    threading.Thread(target=_send, daemon=True).start()


# ------------------------------------------------------------- Plays ----
WELCOME_SUBJECT = "Welcome to The Sharp Team \U0001F988 \u2014 Platinum starts with a $1 week"

WELCOME_TEXT = """Hi __NOMBRE__,

Welcome to The Sharp Team! Your dashboard is ready \u2014 and Platinum starts with a $1 first week.

YOUR ACCESS, IN PLAIN WORDS:
- PLATINUM plays: start for $1 \u2014 your first 7 days of full Platinum cost a single dollar (then $15.99/week only if you stay). Our most valuable play \u2014 the best pick of the day, every day.
- GOLD plays: FREE FOR LIFE \u2014 no card, no expiration, yours forever.

EVERYTHING YOUR PLAN UNLOCKS
1. THE DAILY CARD - Every day at 1:00 PM ET: the day's official plays (Gold plus the one Platinum), each with its original bet ticket. No filler, ever.
2. YOUR PERSONAL TRACKER - One-tap tracking: mark each play, log won or lost. Verified history, real record, net units, ROI and streak. No spreadsheets.
3. LIVE +EV BOARD - Value edges updated around the clock, measured against Pinnacle's no-vig fair price, with sharp-confirmed badges.
4. PUSH ALERTS - Alerts the moment the card drops and when new edges appear. Turn "Alerts on" in your dashboard.
5. TELEGRAM CHANNELS - Gold plays in the "Gold" channel, Platinum plays in the private "Platinum" channel, each with the original ticket.
6. DAILY ACADEMY - One sharp betting lesson every morning: discipline, bankroll management, reading lines and edges.

"Every play is posted before game time - then locked and graded against the final score."

START PLATINUM FOR $1:
__PAY_URL__
Platinum is not automatic \u2014 it starts when you do. One dollar unlocks your first 7 days of full Platinum; after that it is $15.99/week. Cancel anytime. No questions asked.

MAKE SURE YOU DON'T MISS A PLAY:
- Join the Telegram channels: Gold (free plays) https://t.me/thesharpteam - Platinum (private) https://t.me/+_eNhcbD3k4RlNTk5
- Turn on push notifications: open your dashboard and tap "Alerts on".

Your free Gold plays are already live on your dashboard:
https://www.thesharpteam.bet/home

- The Sharp Team

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

WELCOME_HTML = '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Welcome to The Sharp Team</title></head>\n<body style="margin:0;padding:0;background:#05081a;font-family:-apple-system,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%;text-size-adjust:100%;">\n<div style="display:none;font-size:1px;line-height:1px;max-height:0;overflow:hidden;">Welcome to The Sharp Team — You\'re in. Here is how everything works and what to expect today.</div>\n<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#05081a;">\n<tr><td align="center" style="padding:0;">\n<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">\n  <tr><td bgcolor="#0c1638" style="background:linear-gradient(180deg,#05081a 0%,#0c1638 55%,#0a1230 100%);padding:26px 22px 8px;">\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>\n      <td valign="middle"><img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" width="46" alt="The Sharp Team" style="display:block;"></td>\n      <td valign="middle" style="padding-left:10px;"><div style="color:#ffffff;font-size:15.5px;font-weight:800;letter-spacing:2px;">THE SHARP TEAM</div><div style="color:#2ee6a8;font-size:10.5px;font-weight:800;letter-spacing:2px;margin-top:1px;">MEMBER DASHBOARD</div></td>\n      <td align="right" valign="middle"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:10px;font-weight:900;letter-spacing:1.5px;padding:5px 11px;border-radius:999px;">&#8226; ACTIVE</span></td>\n    </tr></table>\n    <div style="color:#ffffff;font-size:56px;line-height:1.15;font-weight:900;margin-top:22px;">You\'re in.</div>\n    <div style="color:#e8c766;font-size:16.5px;font-weight:700;margin-top:3px;">Welcome to the team.</div>\n    <p style="font-size:14.5px;line-height:1.6;color:#b9c4dc;margin:11px 0 0;">In the next few minutes we\'ll walk you through how everything works, what to expect today and where to find every tool, so you can get the most out of your membership from day one.</p>\n    <div style="margin-top:16px;"><a href="https://www.thesharpteam.bet/home" style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:14.5px;font-weight:900;padding:13px 28px;border-radius:999px;text-decoration:none;">OPEN TODAY\'S CARD &rarr;</a></div>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-radius:20px;margin-top:20px;">\n      <tr>\n        <td width="33%" style="padding:13px 4px 11px;text-align:center;"><div style="color:#ffffff;font-size:20px;font-weight:900;white-space:nowrap;">10&ndash;3</div><div style="color:#8fa1c7;font-size:10px;font-weight:700;letter-spacing:1.5px;margin-top:2px;">LAST WEEK</div></td>\n        <td width="34%" style="padding:13px 4px 11px;text-align:center;border-left:1px solid #26355e;border-right:1px solid #26355e;"><div style="color:#2ee6a8;font-size:20px;font-weight:900;white-space:nowrap;">+2.20u</div><div style="color:#8fa1c7;font-size:10px;font-weight:700;letter-spacing:1.5px;margin-top:2px;">UNITS</div></td>\n        <td width="33%" style="padding:13px 4px 11px;text-align:center;"><div style="color:#ffffff;font-size:20px;font-weight:900;white-space:nowrap;">76.9%</div><div style="color:#8fa1c7;font-size:10px;font-weight:700;letter-spacing:1.5px;margin-top:2px;">WIN RATE</div></td>\n      </tr>\n    </table>\n    <div style="font-size:11.5px;color:#8fa1c7;margin-top:9px;">Program totals: 34&ndash;16 &middot; +4.31u &middot; 68.0% win rate &middot; 1u = $100 &middot; Graded publicly. Past results do not guarantee future performance.</div>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:16px 22px 0;">\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-radius:20px;">\n      <tr><td style="padding:16px 18px;">\n        <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">A NOTE FROM THE TEAM</div>\n        <div style="color:#ffffff;font-size:18.5px;font-weight:900;margin-top:6px;">Why we do everything in public</div>\n        <p style="font-size:14.5px;line-height:1.6;color:#b9c4dc;margin:9px 0 0;">Anyone can post winners after the fact. We built The Sharp Team around the opposite idea: every play is posted before game time, backed by a verified ticket, and graded in public, wins and losses alike.</p>\n        <p style="font-size:14.5px;line-height:1.6;color:#b9c4dc;margin:9px 0 0;">That means you never have to take our word for it. The full record is open, ticket by ticket. Your job is simple: read the card, compare prices, and decide what fits your plan.</p>\n        <div style="font-size:14.5px;color:#ffffff;margin-top:11px;"><b>&mdash; The Sharp Team</b></div>\n      </td></tr>\n    </table>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:18px 22px 0;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">YOUR TOOLBOX</div>\n    <div style="color:#ffffff;font-size:20px;font-weight:900;margin-top:5px;">Everything you now have access to</div>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">01</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">Today\'s Card</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">Our official plays, posted every day at 1:00&zwnj; PM ET. The heart of your membership.</div></td></tr></table></td></tr>\n      <tr><td style="height:14px;line-height:14px;font-size:14px;mso-line-height-rule:exactly;">&nbsp;</td></tr>\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">02</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">Compare Prices</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">The best price for each of today\'s plays across every book, so you shop the line.</div></td></tr></table></td></tr>\n      <tr><td style="height:14px;line-height:14px;font-size:14px;mso-line-height-rule:exactly;">&nbsp;</td></tr>\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">03</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">Value Plays</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">Extra value plays beyond the main card, tracked at $100 each.</div></td></tr></table></td></tr>\n      <tr><td style="height:14px;line-height:14px;font-size:14px;mso-line-height-rule:exactly;">&nbsp;</td></tr>\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">04</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">Official Tracker</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">The program\'s complete record, ticket by ticket. No edits, no hiding.</div></td></tr></table></td></tr>\n      <tr><td style="height:14px;line-height:14px;font-size:14px;mso-line-height-rule:exactly;">&nbsp;</td></tr>\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">05</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">My Own Tracker</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">Your personal record on the plays you choose to track, separate from ours.</div></td></tr></table></td></tr>\n      <tr><td style="height:14px;line-height:14px;font-size:14px;mso-line-height-rule:exactly;">&nbsp;</td></tr>\n      <tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="44" valign="top"><span style="display:inline-block;background:#e8c766;color:#1a1405;font-size:12.5px;font-weight:900;padding:6px 9px;border-radius:9px;">06</span></td><td valign="top"><div style="font-size:16px;font-weight:900;color:#ffffff;">Archive</div><div style="font-size:13.5px;line-height:1.5;color:#b9c4dc;margin-top:3px;">Every settled play, day by day. Perfect for reviewing what happened.</div></td></tr></table></td></tr>\n    </table>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0 2px;"><tr><td bgcolor="#9ff7d4" height="3" style="height:3px;font-size:0;line-height:0;border-top-left-radius:3px;border-top-right-radius:3px;">&nbsp;</td></tr><tr><td bgcolor="#2ee6a8" height="6" style="height:6px;font-size:0;line-height:0;">&nbsp;</td></tr><tr><td bgcolor="#0b8a5f" height="3" style="height:3px;font-size:0;line-height:0;border-bottom-left-radius:3px;border-bottom-right-radius:3px;">&nbsp;</td></tr></table>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:18px 22px 0;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">YOUR FIRST DAY</div>\n    <div style="color:#ffffff;font-size:20px;font-weight:900;margin-top:5px;">What to expect, hour by hour</div>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">\n      <tr><td width="26" valign="top" style="padding-top:2px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#2ee6a8;"></span></td><td style="padding:0 0 12px;border-bottom:1px solid #26355e;"><span style="color:#e8c766;font-size:10.5px;font-weight:800;letter-spacing:1.5px;">MORNING</span><div style="font-size:15.5px;font-weight:800;color:#ffffff;margin-top:3px;">Get set up</div><div style="font-size:14px;line-height:1.55;color:#b9c4dc;margin-top:2px;">Open the app and turn on Alerts so you know the moment the card is live.</div></td></tr>\n      <tr><td width="26" valign="top" style="padding-top:12px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#2ee6a8;"></span></td><td style="padding:10px 0 12px;border-bottom:1px solid #26355e;"><span style="color:#e8c766;font-size:10.5px;font-weight:800;letter-spacing:1.5px;">1:00&zwnj; PM ET</span><div style="font-size:15.5px;font-weight:800;color:#ffffff;margin-top:3px;">The card posts</div><div style="font-size:14px;line-height:1.55;color:#b9c4dc;margin-top:2px;">Today\'s Card goes live with our official plays.</div></td></tr>\n      <tr><td width="26" valign="top" style="padding-top:12px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#2ee6a8;"></span></td><td style="padding:10px 0 12px;border-bottom:1px solid #26355e;"><span style="color:#e8c766;font-size:10.5px;font-weight:800;letter-spacing:1.5px;">BEFORE GAME</span><div style="font-size:15.5px;font-weight:800;color:#ffffff;margin-top:3px;">Shop the price</div><div style="font-size:14px;line-height:1.55;color:#b9c4dc;margin-top:2px;">Use Compare Prices to find the best number across every book before you place anything.</div></td></tr>\n      <tr><td width="26" valign="top" style="padding-top:12px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#2ee6a8;"></span></td><td style="padding:10px 0 2px;"><span style="color:#e8c766;font-size:10.5px;font-weight:800;letter-spacing:1.5px;">AFTER GAMES</span><div style="font-size:15.5px;font-weight:800;color:#ffffff;margin-top:3px;">Results get graded</div><div style="font-size:14px;line-height:1.55;color:#b9c4dc;margin-top:2px;">Plays are settled publicly and added to the Official Tracker and Archive.</div></td></tr>\n    </table>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:18px 22px 0;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">GLOSSARY</div>\n    <div style="color:#ffffff;font-size:20px;font-weight:900;margin-top:5px;">How to read our results</div>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">\n      <tr><td style="padding:7px 0;border-bottom:1px solid #26355e;font-size:14.5px;line-height:1.55;color:#b9c4dc;"><b style="color:#ffffff;">Unit (u)</b> &mdash; Our standard bet size. In our tracking 1u = $100, which keeps every result on the same scale.</td></tr>\n      <tr><td style="padding:7px 0;border-bottom:1px solid #26355e;font-size:14.5px;line-height:1.55;color:#b9c4dc;"><b style="color:#ffffff;">Record</b> &mdash; Wins and losses across graded plays, like 10&ndash;3 last week.</td></tr>\n      <tr><td style="padding:7px 0;border-bottom:1px solid #26355e;font-size:14.5px;line-height:1.55;color:#b9c4dc;"><b style="color:#ffffff;">Win rate</b> &mdash; Wins divided by total graded plays: 76.9% last week, 68.0% across the program.</td></tr>\n      <tr><td style="padding:7px 0;font-size:14.5px;line-height:1.55;color:#b9c4dc;"><b style="color:#ffffff;">Graded publicly</b> &mdash; Every play is settled in the open and appears in the Official Tracker. Nothing is deleted.</td></tr>\n    </table>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:18px 22px 0;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">OUR STANDARDS</div>\n    <div style="color:#ffffff;font-size:20px;font-weight:900;margin-top:5px;">Three promises, every day</div>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #1e5c46;border-left:6px solid #2ee6a8;border-radius:20px;margin-top:12px;">\n      <tr><td style="padding:14px 18px;">\n        <div style="font-size:14.5px;color:#2ee6a8;padding:3px 0;">&#10003;&nbsp; <b style="color:#ffffff;">Posted before game time</b> <span style="color:#b9c4dc;">&mdash; No after-the-fact picks.</span></div>\n        <div style="font-size:14.5px;color:#2ee6a8;padding:3px 0;">&#10003;&nbsp; <b style="color:#ffffff;">Verified ticket</b> <span style="color:#b9c4dc;">&mdash; A ticket on every play we post.</span></div>\n        <div style="font-size:14.5px;color:#2ee6a8;padding:3px 0;">&#10003;&nbsp; <b style="color:#ffffff;">Public record</b> <span style="color:#b9c4dc;">&mdash; Wins and losses in the open.</span></div>\n      </td></tr>\n    </table>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:18px 22px 0;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">FAQ</div>\n    <div style="color:#ffffff;font-size:20px;font-weight:900;margin-top:5px;">Questions new members ask</div>\n    <div style="margin-top:12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="36" valign="top"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:15px;font-weight:900;width:25px;height:25px;line-height:25px;text-align:center;border-radius:50%;">+</span></td><td valign="top"><div style="font-size:15.5px;font-weight:800;color:#ffffff;">When do plays post?</div><div style="font-size:14.5px;line-height:1.55;color:#b9c4dc;margin-top:4px;">Every day at 1:00&zwnj; PM ET in Today\'s Card.</div></td></tr></table></td></tr></table><div style="height:10px;line-height:10px;font-size:10px;">&nbsp;</div><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="36" valign="top"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:15px;font-weight:900;width:25px;height:25px;line-height:25px;text-align:center;border-radius:50%;">+</span></td><td valign="top"><div style="font-size:15.5px;font-weight:800;color:#ffffff;">Do I have to play everything?</div><div style="font-size:14.5px;line-height:1.55;color:#b9c4dc;margin-top:4px;">No. Our plays are information. Choose what fits your budget and plan, and bet only what you can afford to lose.</div></td></tr></table></td></tr></table><div style="height:10px;line-height:10px;font-size:10px;">&nbsp;</div><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="36" valign="top"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:15px;font-weight:900;width:25px;height:25px;line-height:25px;text-align:center;border-radius:50%;">+</span></td><td valign="top"><div style="font-size:15.5px;font-weight:800;color:#ffffff;">What are Value Plays?</div><div style="font-size:14.5px;line-height:1.55;color:#b9c4dc;margin-top:4px;">Extra plays tracked at $100 each, listed separately from the main card.</div></td></tr></table></td></tr></table><div style="height:10px;line-height:10px;font-size:10px;">&nbsp;</div><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="36" valign="top"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:15px;font-weight:900;width:25px;height:25px;line-height:25px;text-align:center;border-radius:50%;">+</span></td><td valign="top"><div style="font-size:15.5px;font-weight:800;color:#ffffff;">Where do I see past results?</div><div style="font-size:14.5px;line-height:1.55;color:#b9c4dc;margin-top:4px;">In the Official Tracker (complete record) and the Archive (every settled play).</div></td></tr></table></td></tr></table><div style="height:10px;line-height:10px;font-size:10px;">&nbsp;</div><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#101c3f" style="background:rgba(255,255,255,0.05);border:1px solid #26355e;border-left:6px solid #2ee6a8;border-radius:16px;padding:13px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td width="36" valign="top"><span style="display:inline-block;background:#2ee6a8;color:#04241a;font-size:15px;font-weight:900;width:25px;height:25px;line-height:25px;text-align:center;border-radius:50%;">+</span></td><td valign="top"><div style="font-size:15.5px;font-weight:800;color:#ffffff;">Can I track my own bets?</div><div style="font-size:14.5px;line-height:1.55;color:#b9c4dc;margin-top:4px;">Yes. My Own Tracker keeps your personal record on the plays you track.</div></td></tr></table></td></tr></table></div>\n  </td></tr>\n  <tr><td bgcolor="#0c1638" style="background:#0a1230;padding:20px 22px 26px;">\n    <div style="color:#e8c766;font-size:11px;font-weight:800;letter-spacing:2.5px;">READY?</div>\n    <div style="color:#ffffff;font-size:22px;font-weight:900;margin-top:6px;">Your first card is waiting.</div>\n    <p style="font-size:14.5px;line-height:1.6;color:#b9c4dc;margin:10px 0 0;">Check today\'s plays, then take a look at the Academy, our 30-day course on how the Sharp Team works.</p>\n    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;"><tr><td width="50%" valign="middle" bgcolor="#e8c766" style="background:#e8c766;border-radius:999px;"><a href="https://www.thesharpteam.bet/home" style="display:block;text-align:center;color:#1a1405;font-size:12.5px;font-weight:900;padding:13px 6px;text-decoration:none;">VIEW TODAY\'S CARD &rarr;</a></td><td width="10" style="font-size:0;line-height:0;">&nbsp;</td><td width="50%" valign="middle" bgcolor="#e8c766" style="background:#e8c766;border-radius:999px;"><a href="https://www.thesharpteam.bet/masterclass" style="display:block;text-align:center;color:#1a1405;font-size:12.5px;font-weight:900;padding:13px 6px;text-decoration:none;">Start the 30-Day Academy &rarr;</a></td></tr></table>\n    <p style="font-size:12px;line-height:1.55;color:#7d8ca3;margin:18px 0 0;"><b style="color:#b9c4dc;">RESPONSIBLE PLAY</b> &mdash; Only bet what you can afford to lose and set limits that work for you. If you or someone you know has a gambling problem, call 1-800-GAMBLER or visit <a href="https://www.800gambler.org" style="color:#b9c4dc;text-decoration:underline;">800gambler.org</a>.</p>\n  </td></tr>\n  <tr><td bgcolor="#05081a" style="background:#05081a;border-radius:0 0 16px 16px;padding:20px 24px;text-align:center;">\n    <img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" width="54" alt="The Sharp Team" style="display:block;margin:0 auto 6px;">\n    <div style="font-size:13px;font-weight:800;color:#ffffff;letter-spacing:1px;">THE SHARP TEAM</div>\n    <div style="font-size:12px;color:#94a3b8;margin-top:4px;">250 Park Avenue, Suite 1800, New York, NY 10017 &middot; (551) 326-3312</div>\n    <div style="margin-top:12px;">\n      <a href="https://www.instagram.com/the_sharp_team" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/instagram.png" width="30" height="30" alt="Instagram" style="border:0;margin:0 6px;"></a>\n      <a href="https://x.com/thesharpteambet" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/x.png" width="30" height="30" alt="X" style="border:0;margin:0 6px;"></a>\n      <a href="https://t.me/thesharpteam" style="text-decoration:none;"><img src="https://www.thesharpteam.bet/static/img/icons/telegram.png" width="30" height="30" alt="Telegram" style="border:0;margin:0 6px;"></a>\n    </div>\n    <div style="font-size:11px;color:#94a3b8;line-height:1.5;margin-top:10px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text 1-800-GAMBLER.</div>\n    <div style="font-size:11px;color:#94a3b8;margin-top:8px;">Don\'t want these emails anymore? <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#94a3b8;text-decoration:underline;">Unsubscribe</a> — one click, no questions asked.</div>\n    <div style="font-size:11px;margin-top:8px;"><a href="https://www.thesharpteam.bet/contact" style="color:#94a3b8;">Support</a> &middot; <a href="https://www.thesharpteam.bet/privacy" style="color:#94a3b8;">Privacy Policy</a></div>\n    <div style="font-size:11px;color:#64748b;line-height:1.5;margin-top:8px;">Must be 21+. The Sharp Team provides information and entertainment only and is not a sportsbook. Past results do not guarantee future performance. 1u = $100.</div>\n    <div style="font-size:11px;color:#64748b;margin-top:6px;">&copy; 2026 The Sharp Team. All rights reserved.</div>\n  </td></tr>\n</table>\n</td></tr></table>\n</body></html>\n'

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
    text = WELCOME_TEXT.replace("__NOMBRE__", nombre).replace("__PAY_URL__", STRIPE_PLATINUM_URL)
    html = WELCOME_HTML.replace("__NOMBRE__", nombre).replace("__PAY_URL__", STRIPE_PLATINUM_URL)

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

Your last membership payment did not go through, so your Platinum plays are paused.

Nothing is canceled and nothing was missed on your end - cards just fail sometimes. Today's card is posted and waiting. Fix the card and everything switches back on instantly, usually within a couple of minutes.

Fix your card in one tap (takes about 30 seconds):
__PAY_URL__

Your Gold plays stay on while the card is sorted - that is part of every membership. Your Platinum plays unlock the moment the payment lands. If anything looks off, just reply to this email and a real person will sort it.

- The Sharp Team

Service notice about your membership billing.

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

DUNNING_HTML = """<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;background:#ffffff;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-banner.png" alt="The Sharp Team" width="480" style="width:100%;height:auto;display:block;border:0;">
<div style="background:#ffffff;height:18px;line-height:18px;font-size:1px;">&nbsp;</div>
<div style="background:#2e4053;padding:28px 24px;text-align:center;">
<p style="margin:0;font-size:12px;font-weight:800;letter-spacing:2px;color:#9aa3b8;">&#9888;&nbsp;PAYMENT ISSUE</p>
<h1 style="font-size:30px;font-weight:800;color:#ffffff;margin:10px 0 0;line-height:1.25;">Today&apos;s card is set.<br><span style="color:#22c55e;">Your card isn&apos;t.</span></h1>
</div>
<div style="margin:14px 24px 0;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:18px;">
<p style="margin:0;font-size:15px;font-weight:700;color:#111111;line-height:1.55;">Hi __NOMBRE__ &mdash; your last payment didn&apos;t go through, so your Platinum plays are on hold.</p>
<p style="margin:10px 0 0;font-size:14px;color:#555555;line-height:1.6;">Nothing is canceled &mdash; cards just fail sometimes. Fix it now and everything switches back on instantly.</p>
</div>
<div style="text-align:center;padding:16px 24px 0;">
<a href="__PAY_URL__" style="display:inline-block;background:#22c55e;color:#111111;font-weight:800;font-size:16px;padding:14px 36px;border-radius:999px;text-decoration:none;">Fix my card in one tap &rarr;</a>
<p style="margin:10px 0 0;font-size:13px;color:#6b7280;">Takes about 30 seconds &middot; Your Gold plays stay yours either way.</p>
</div>
<p style="font-size:13px;line-height:1.6;color:#6b7280;margin:16px 24px 0;text-align:center;">If anything looks off, just reply to this email and a real person will sort it.</p>
<p style="text-align:center;font-size:12px;color:#9ca3af;margin:14px 0 0;">__FECHA__</p>
<div style="text-align:center;padding:20px 20px 14px;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" alt="The Sharp Team" width="110" style="width:110px;height:auto;display:block;margin:0 auto;border:0;">
<p style="margin:10px 0 0;color:#111111;font-size:12px;font-weight:800;letter-spacing:3px;">THE SHARP TEAM</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:11px;">250 Park Avenue, Suite 1800, New York, NY 10017</p>
<p style="margin:4px 0 0;color:#9ca3af;font-size:11px;"><a href="tel:+15513263312" style="color:#9ca3af;text-decoration:none;">(551) 326-3312</a></p>
<p style="margin:12px 0 0;color:#9ca3af;font-size:10px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text <a href="tel:18004262537" style="color:#9ca3af;text-decoration:underline;">1-800-GAMBLER</a>.</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:10px;">Don't want these emails anymore? <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#9ca3af;text-decoration:underline;">Unsubscribe</a> — one click, no questions asked.</p>
<p style="margin:6px 0 0;color:#9ca3af;font-size:10px;">&copy; 2026 The Sharp Team</p>
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


MONTHLY_SUBJECT = "Your {mes} statement is here \U0001F4CA"

MONTHLY_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>Your monthly statement</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">Your {mes} statement &mdash; every play graded, every dollar accounted.&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;</div>
<div style="font-family:Arial,sans-serif;max-width:460px;margin:0 auto;background:#ffffff;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-banner.png" alt="The Sharp Team" width="460" style="width:100%;height:auto;display:block;border:0;">
<div style="background:#ffffff;padding:14px 0 12px;text-align:center;">
<p style="margin:0;font-size:13px;font-weight:800;letter-spacing:5px;color:#2e4053;">THE SHARP TEAM</p>
</div>
<div style="background:#c9a227;padding:28px 24px;text-align:center;">
<p style="margin:0;font-size:12px;font-weight:800;letter-spacing:3px;color:#0a0f1c;">MONTHLY STATEMENT</p>
<h1 style="font-size:30px;font-weight:800;color:#0a0f1c;margin:10px 0 0;line-height:1.25;">Your {mes} statement.</h1>
</div>
<div style="text-align:center;padding:16px 20px 0;">
<p style="color:#555555;font-size:15px;line-height:1.6;margin:0;">Hi __NOMBRE__ &mdash; here is how your {mes} closed. Every play graded, every dollar accounted.</p>
</div>
<div style="padding:14px 20px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="50%" style="padding:0 5px 10px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;"><tr><td style="padding:14px;text-align:center;">
<p style="margin:0;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">RECORD</p>
<p style="margin:6px 0 0;font-size:22px;font-weight:800;color:#111111;">__RECORD__</p>
</td></tr></table>
</td>
<td width="50%" style="padding:0 0 10px 5px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;"><tr><td style="padding:14px;text-align:center;">
<p style="margin:0;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">WIN RATE</p>
<p style="margin:6px 0 0;font-size:22px;font-weight:800;color:#111111;">__WINRATE__</p>
</td></tr></table>
</td>
</tr><tr>
<td width="50%" style="padding:0 5px 10px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;"><tr><td style="padding:14px;text-align:center;">
<p style="margin:0;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">NET PROFIT</p>
<p style="margin:6px 0 0;font-size:22px;font-weight:800;color:__PROFIT_COLOR__;">__PROFIT__</p>
</td></tr></table>
</td>
<td width="50%" style="padding:0 0 10px 5px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;"><tr><td style="padding:14px;text-align:center;">
<p style="margin:0;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">ROI</p>
<p style="margin:6px 0 0;font-size:22px;font-weight:800;color:__PROFIT_COLOR__;">__ROI__</p>
</td></tr></table>
</td>
</tr></table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:2px 0 0;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;"><tr><td style="padding:14px 16px;">
<p style="margin:0;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">BY LEVEL</p>
<p style="margin:8px 0 0;font-size:14px;color:#111111;">&#11088; <strong>Platinum</strong> &mdash; __PLATINUM__</p>
<p style="margin:6px 0 0;font-size:14px;color:#111111;">&#9898; <strong>Gold</strong> &mdash; __GOLD__</p>
</td></tr></table>
</div>
<div style="padding:14px 20px 0;">
<p style="margin:0 0 8px;font-size:11px;font-weight:800;letter-spacing:2px;color:#6b7280;">PLAY BY PLAY</p>
__PLAYS_ROWS__
</div>
<div style="text-align:center;padding:18px 20px 0;">
<a href="https://www.thesharpteam.bet/records" style="display:inline-block;background:#22c55e;color:#111111;font-size:14px;font-weight:800;padding:11px 28px;border-radius:999px;text-decoration:none;">View full record &rarr;</a>
</div>
<div style="text-align:center;padding:20px 20px 14px;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-logo.png" alt="The Sharp Team" width="110" style="width:110px;height:auto;display:block;margin:0 auto;border:0;">
<p style="margin:10px 0 0;color:#111111;font-size:12px;font-weight:800;letter-spacing:3px;">THE SHARP TEAM</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:11px;">250 Park Avenue, Suite 1800, New York, NY 10017</p>
<p style="margin:4px 0 0;color:#9ca3af;font-size:11px;"><a href="tel:+15513263312" style="color:#9ca3af;text-decoration:none;">(551) 326-3312</a></p>
<p style="margin:12px 0 0;color:#9ca3af;font-size:10px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text <a href="tel:18004262537" style="color:#9ca3af;text-decoration:underline;">1-800-GAMBLER</a>.</p>
<p style="margin:8px 0 0;color:#9ca3af;font-size:10px;">Don't want these emails anymore? <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#9ca3af;text-decoration:underline;">Unsubscribe</a> — one click, no questions asked.</p>
<p style="margin:6px 0 0;color:#9ca3af;font-size:10px;">&copy; 2026 The Sharp Team</p>
</div>
</div>
</body>
</html>
"""

MONTHLY_TEXT = """Hi __NOMBRE__ — here is your {mes} statement from The Sharp Team.

Record: __RECORD__ (__WINRATE__ win rate)
Net profit: __PROFIT__
ROI: __ROI__

Platinum: __PLATINUM__
Gold: __GOLD__

View your full record: https://www.thesharpteam.bet/records

21+. Play responsibly. If you or someone you know has a gambling problem, call or text 1-800-GAMBLER.
(c) 2026 The Sharp Team
"""


def _month_name_es_month(ym: str) -> str:
    """'2026-09' -> 'September' (nombre del mes en inglés para el email)."""
    import calendar
    y, m = ym.split("-")
    return calendar.month_name[int(m)]


def monthly_member_stats(db, user_id: int, ym: str) -> dict:
    """Stats reales del miembro para el mes YYYY-MM desde tracked_plays."""
    rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? AND substr(fecha, 1, 7) = ? "
        "ORDER BY fecha, id",
        (user_id, ym),
    ).fetchall()
    tracked = [dict(r) for r in rows]
    graded = [t for t in tracked if t["resultado"] in ("W", "L")]
    base = compute_stats(tracked)
    # Desglose por nivel
    niveles = {}
    for lvl in ("ELITE", "GOLD"):
        g = [t for t in graded if (t["nivel"] or "").upper() in (lvl, "PLATINUM" if lvl == "ELITE" else lvl)]
        w = sum(1 for t in g if t["resultado"] == "W")
        p = sum(play_profit_dollars(t) for t in g)
        tag = "Platinum" if lvl == "ELITE" else "Gold"
        niveles[tag] = f"{w}-{len(g) - w}, {'+' if p >= 0 else '-'}$ {abs(p):,.2f}".replace("$-", "-$")
    win_rate = (base["wins"] / (base["wins"] + base["losses"]) * 100) if (base["wins"] + base["losses"]) else 0.0
    profit = base["net_dollars"]
    plays_out = []
    for t in graded:
        d = dict(t)
        d["_profit"] = play_profit_dollars(t)
        plays_out.append(d)
    return {
        "plays": plays_out,
        "record": base["record"],
        "win_rate": f"{win_rate:.1f}%",
        "profit": profit,
        "profit_str": f"${profit:,.2f}" if profit >= 0 else f"-${abs(profit):,.2f}",
        "profit_color": "#16a34a" if profit >= 0 else "#dc2626",
        "roi": f"{base['roi']:.1f}%",
        "risked": base["risked"],
        "platinum": niveles.get("Platinum", "0-0, $0.00"),
        "gold": niveles.get("Gold", "0-0, $0.00"),
    }


def build_monthly_html(nombre: str, ym: str, st: dict) -> str:
    """Arma el HTML de la liquidación mensual con los números reales."""
    mes = _month_name_es_month(ym)
    rows = []
    for t in st["plays"]:
        p = t["_profit"] if "_profit" in t else play_profit_dollars(t)
        color = "#16a34a" if p >= 0 else "#dc2626"
        marca = "&#10003;" if t["resultado"] == "W" else "&#10007;"
        nivel = "PLATINUM" if (t["nivel"] or "").upper() in ("ELITE", "PLATINUM") else "GOLD"
        rows.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;">'
            f'<tr><td style="padding:10px 14px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td><span style="font-size:14px;font-weight:800;color:#111111;">{t["pick"]}</span> '
            f'<span style="font-size:12px;color:#6b7280;">{t["cuota"]:+d} &middot; {nivel}</span><br>'
            f'<span style="font-size:12px;color:#6b7280;">{t["fecha"]}</span></td>'
            f'<td align="right" style="font-size:14px;font-weight:800;color:{color};">{marca} '
            f'{"+" if p >= 0 else "-"}${abs(p):,.2f}</td>'
            f'</tr></table></td></tr></table>'
        )
    plays_html = "\n".join(rows) if rows else (
        '<p style="font-size:14px;color:#6b7280;">No graded plays this month.</p>'
    )
    html = (MONTHLY_HTML
            .replace("{mes}", mes)
            .replace("__NOMBRE__", nombre or "there")
            .replace("__RECORD__", st["record"])
            .replace("__WINRATE__", st["win_rate"])
            .replace("__PROFIT__", st["profit_str"])
            .replace("__PROFIT_COLOR__", st["profit_color"])
            .replace("__ROI__", st["roi"])
            .replace("__PLATINUM__", st["platinum"])
            .replace("__GOLD__", st["gold"])
            .replace("__PLAYS_ROWS__", plays_html))
    return html


def send_monthly_statement(email: str, ym: str) -> tuple:
    """Genera y envía la liquidación mensual al miembro (números reales de su tracker).

    Devuelve (ok, detalle). No lanza excepciones.
    """
    try:
        db = get_db()
        row = db.execute("SELECT id, nombre FROM users WHERE LOWER(email) = ?",
                         (email.strip().lower(),)).fetchone()
        if not row:
            return (False, f"No existe cuenta con {email}")
        st = monthly_member_stats(db, row["id"], ym)
        if not st["plays"]:
            return (False, f"Sin jugadas liquidadas en {ym} para {email}")
        mes = _month_name_es_month(ym)
        html = build_monthly_html(row["nombre"] or "", ym, st)
        text = (MONTHLY_TEXT
                .replace("{mes}", mes)
                .replace("__NOMBRE__", row["nombre"] or "there")
                .replace("__RECORD__", st["record"])
                .replace("__WINRATE__", st["win_rate"])
                .replace("__PROFIT__", st["profit_str"])
                .replace("__ROI__", st["roi"])
                .replace("__PLATINUM__", st["platinum"])
                .replace("__GOLD__", st["gold"]))
        subject = MONTHLY_SUBJECT.replace("{mes}", mes)
        ok, detail = _dispatch_email(email, subject, text, html)
        return (ok, detail)
    except Exception as e:
        return (False, f"Error: {e}")



def _json_remoto_o_local(url, path, ttl=30):
    """JSON de datos fresco: primero el raw de GitHub master (caché de
    `ttl` s en memoria), y si la red falla, el archivo local del despliegue.
    Los fallos se reintentan como máximo cada 10 s para no castigar las
    cargas de página durante una caída de GitHub."""
    slot = _REMOTE_JSON_CACHE.setdefault(
        url, {"data": None, "ts": 0.0, "fail_ts": 0.0})
    now = time.time()
    if slot["data"] is not None and now - slot["ts"] < ttl:
        return slot["data"]
    if now - slot["fail_ts"] > 10:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (TheSharpTeam)"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, (dict, list)):
                slot["data"] = data
                slot["ts"] = now
                return data
        except Exception:
            slot["fail_ts"] = now
    if slot["data"] is not None:
        return slot["data"]
    return load_json_file(path)


def load_data():
    """Devuelve (program, plays). Soporta plays.json como lista (viejo) o dict (nuevo)."""
    data = _json_remoto_o_local(PLAYS_RAW_URL, PLAYS_PATH)
    if data is None:
        data = []
    if isinstance(data, dict):
        program = data.get("program", {}) or {}
        plays = data.get("plays", []) or []
    else:
        program = {}
        plays = data if isinstance(data, list) else []
    plays = [p for p in plays if isinstance(p, dict) and p.get("id")]
    return program, plays


def is_alex_member(user) -> bool:
    """True si el usuario es la cuenta personal de miembro de Alex."""
    try:
        return bool(user) and (user["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL
    except (KeyError, IndexError, TypeError):
        return False


def load_pending_plays():
    """Card del día aún sin tickets (data/plays-pending.json).

    Regla de Alex (2026-10-03): solo su cuenta personal de miembro
    (ALEX_MEMBER_EMAIL) ve la card del día en /home SIEMPRE, con o sin
    tickets — es su cuenta de seguimiento personal. Ni el admin ni los
    demás miembros la ven: ellos solo la ven cuando él envía los tickets
    (flujo normal que publica plays.json verificado y limpia este archivo).
    """
    data = _json_remoto_o_local(PENDING_RAW_URL, PENDING_PLAYS_PATH)
    if not isinstance(data, dict):
        return []
    hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    if data.get("fecha") != hoy:
        return []
    plays = data.get("plays", []) or []
    return [p for p in plays if isinstance(p, dict) and p.get("id")]


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
    Acepta cuota como int o string, con o sin signo (ej. 186, "+186", "-110").
    """
    try:
        _edge_raw = play.get("edge") or 0
        if isinstance(_edge_raw, str):
            # plays.json trae el edge como texto ("+6.2%"): sin esta limpieza
            # float() explotaba y stake_kelly devolvía $0 (bug visto por Alex
            # el 2026-10-13: la card mostraba "Bet $0" con ticket de $50).
            _edge_raw = _edge_raw.replace("%", "").replace("EV", "").strip()
        edge = float(_edge_raw) / 100.0
        cuota = str(play.get("cuota") or "").strip().replace("−", "-")
        if cuota.startswith("-"):
            v = float(cuota[1:])
            if v <= 0:
                return 0.0
            b = 100.0 / v
            implied = v / (v + 100.0)
        else:
            # Positiva: con "+" explícito o sin signo (ej. 186)
            v = float(cuota.lstrip("+"))
            if v <= 0:
                return 0.0
            b = v / 100.0
            implied = 100.0 / (v + 100.0)
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
    jugada (1u -> 1%, 0.6u -> 0.6%). Regla 1% estilo WGT, confirmada por Alex
    el 2026-10-03 (se había subido a 2% el 2026-10-02 y se reversó).
    Si no tiene bankroll configurado, se usa el stake oficial
    del programa (stake_monto de plays.json).

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
            # Un stake fijo guardado como "0" (texto truthy) devolvía $0 en la
            # card — misma clase de trampa que el bug del Bet $0 (2026-10-13):
            # solo aplica si el monto fijo es mayor que cero.
            _fijo_e = float(_uget(user, "stake_fijo_elite") or 0)
            _fijo_g = float(_uget(user, "stake_fijo_gold") or 0)
            if nivel == "ELITE" and _fijo_e > 0:
                return round(_fijo_e, 2)
            if nivel == "GOLD" and _fijo_g > 0:
                return round(_fijo_g, 2)
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
    data = _json_remoto_o_local(PLAYS_RAW_URL, PLAYS_PATH)
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



def program_streak():
    """Racha actual del programa (W/L) desde el archivo, la más reciente primero.
    (Pedido Alex 2026-10-02: racha visible en el hero del Dashboard.)"""
    ordered = []
    for d in load_archive():
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada"):
                ordered.append(j["resultado"])
    if not ordered:
        return None
    tipo = ordered[0]
    n = 0
    for r in ordered:
        if r == tipo:
            n += 1
        else:
            break
    return {"tipo": "W" if tipo == "WON" else "L", "n": n}


def win_streak_days():
    """Días ganadores seguidos del programa (franja bajo la barra superior
    v2, pedido Alex 2026-10-21, referencia WGT). Cuenta hacia atrás desde
    el día calificado más reciente de data/archive.json: un día cuenta si
    TODAS sus jugadas oficiales están liquidadas (WON/LOST/PUSH) y su
    resultado neto en unidades es > 0. El neto en unidades de cada jugada
    se deriva de su profit real: profit × stake_unidades ÷ stake_monto.
    Un día aún no calificado por completo no cuenta ni corta la racha;
    el primer día calificado con neto <= 0 la corta. Sin datos: 0."""
    n = 0
    try:
        dias = sorted(
            (d for d in load_archive() if isinstance(d, dict)),
            key=lambda d: d.get("fecha") or "", reverse=True)
    except Exception:
        return 0
    for d in dias:
        jugadas = [j for j in (d.get("jugadas", []) or [])
                   if isinstance(j, dict) and not j.get("bloqueada")]
        if not jugadas:
            continue
        if any(j.get("resultado") not in ("WON", "LOST", "PUSH")
               for j in jugadas):
            continue
        neto = 0.0
        ok = True
        for j in jugadas:
            # Coerción defensiva (2026-10-21): si un script escribe los
            # montos como texto ("100"), float() los rescata; si no son
            # números, el día se salta en vez de explotar — esta función
            # corre en el context processor global y JAMÁS debe tumbar
            # una página.
            try:
                p = float(j.get("profit"))
                sm = float(j.get("stake_monto") or 0)
                su = float(j.get("stake_unidades") or 0)
            except (TypeError, ValueError):
                ok = False
                break
            if not sm or not su:
                ok = False
                break
            neto += p * su / sm
        if not ok:
            continue
        if neto > 0:
            n += 1
        else:
            break
    return n


def performance_snapshot():
    """Snapshot de rendimiento para la sección 'Performance' del Dashboard.

    Diseño 100% original de The Sharp Team (pedido por Alex 2026-10-03):
    muestra últimos 30 días, racha actual, carrera y win rate. Datos reales
    del archivo oficial — nunca inventados.
    """
    hoy = datetime.now(TZ).date()
    hace_30 = hoy - timedelta(days=30)
    w30 = l30 = 0
    u30 = 0.0
    w_all = l_all = 0
    profit_all = 0.0
    ordered = []
    for d in load_archive():
        try:
            f = datetime.strptime(d.get("fecha", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") not in ("WON", "LOST") or j.get("bloqueada"):
                continue
            r = j["resultado"]
            ordered.append(r)
            w_all += 1 if r == "WON" else 0
            l_all += 1 if r == "LOST" else 0
            profit_all += float(j.get("profit") or 0.0)
            if f >= hace_30:
                if r == "WON":
                    w30 += 1
                else:
                    l30 += 1
                u30 += float(j.get("stake_unidades") or 0.0) * (1 if r == "WON" else -1)
    streak = program_streak() or {"tipo": "—", "n": 0}
    settled = w_all + l_all
    wr = round(w_all / settled * 100, 1) if settled else 0.0
    pn = int(round(profit_all))
    return {
        "u30": round(u30, 1),
        "u30_cls": "pos" if u30 > 0 else ("neg" if u30 < 0 else ""),
        "w30": w30,
        "l30": l30,
        "streak_tipo": streak["tipo"],
        "streak_n": streak["n"],
        "profit_display": ("+$" if pn > 0 else ("-$" if pn < 0 else "$")) + f"{abs(pn):,}",
        "profit_cls": "pos" if pn > 0 else ("neg" if pn < 0 else ""),
        "w_all": w_all,
        "l_all": l_all,
        "win_rate": wr,
    }


def resumen_ayer():
    """Récord y profit del día calendario anterior (America/New_York).
    Se muestra en el hero cuando la card de hoy aún no sale. (Pedido Alex 2026-10-02.)"""
    ayer = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    for d in load_archive():
        if d.get("fecha") == ayer:
            js = [j for j in (d.get("jugadas", []) or []) if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada")]
            if not js:
                return None
            w = sum(1 for j in js if j["resultado"] == "WON")
            l = len(js) - w
            p = round(sum(float(j.get("profit") or 0) for j in js), 2)
            return {"w": w, "l": l, "profit": p}
    return None


def celebracion_hoy():
    """Lluvia de dinero del dashboard (pedido de Alex 2026-10-13): si AYER
    cerró positivo devuelve {w, l, profit} para la burbuja de resultados;
    la celebración termina cuando empieza el primer partido de HOY (se
    busca la hora más temprana entre la card publicada y la pendiente de
    hoy). Si ayer no fue positivo o ya empezó la acción de hoy: None."""
    r = resumen_ayer()
    if not r or r["profit"] <= 0:
        return None
    try:
        ahora = datetime.now(TZ)
        hoy = ahora.date().isoformat()
        inicios = []
        fuentes = []
        for ruta in (PLAYS_PATH, PENDING_PLAYS_PATH):
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    fuentes.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                pass
        for fuente in fuentes:
            if not isinstance(fuente, dict) or fuente.get("fecha") != hoy:
                continue
            for p in fuente.get("plays", []) or []:
                hora = (p.get("hora") or "").replace("ET", "").strip()
                for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
                    try:
                        t = datetime.strptime(hora, fmt)
                        inicios.append(ahora.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0))
                        break
                    except ValueError:
                        continue
        if inicios and ahora >= min(inicios):
            return None
    except Exception:
        pass
    return r


# Ligas que cubrimos: clasificación del pick por equipo (desglose por liga).
def _cierre_txt(j):
    """Precio de cierre formateado (+120 / -107) desde cuota_cierre de la
    jugada; cadena vacía si la jugada no tiene cierre verificado."""
    c = j.get("cuota_cierre")
    if c in (None, "", 0):
        return ""
    try:
        return f"{int(float(c)):+d}"
    except (TypeError, ValueError):
        return ""


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
                # Unidades de profit (2026-10-15): la fila de Receipts
                # lidera con unidades estilo WGT (+1.56u) y el $ va debajo.
                _su = j.get("stake_unidades")
                _sm = j.get("stake_monto")
                _unidades = None
                try:
                    if _su and _sm:
                        _unidades = round(float(j.get("profit") or 0) * float(_su) / float(_sm), 2)
                except (TypeError, ValueError):
                    _unidades = None
                recientes.append({
                    "id": j.get("id"),
                    "fecha": d.get("titulo") or d.get("fecha", ""),
                    "fecha_iso": d.get("fecha", ""),
                    "liga": j.get("liga", ""),
                    "abbr": j.get("abbr", ""),
                    "opp_abbr": j.get("opp_abbr", ""),
                    "nivel": j.get("nivel", ""),
                    "pick": j.get("pick", ""),
                    "cuota": j.get("cuota"),
                    "resultado": j.get("resultado"),
                    "profit": j.get("profit") if j.get("profit") is not None else 0.0,
                    "comprobante": j.get("comprobante"), "cierre": _cierre_txt(j), "clv": j.get("clv_pct"),
                    "stake_unidades": _su,
                    "unidades": _unidades,
                })
    return recientes[:n]


def recientes_value(n=20):
    """Jugadas VALUE del sistema (simuladas), más recientes primero.
    Para el historial desplegable de la sección VALUE.
    Devuelve dict con 'actuales' (en vivo/pending o del día, van visibles)
    y 'pasadas' (ya terminadas de días anteriores, van bajo "View all").
    (2026-10-04, pedido por Alex)."""
    vacio = {"actuales": [], "pasadas": [], "todas": []}
    try:
        from datetime import datetime
        hoy = datetime.now(TZ).strftime("%Y-%m-%d")
        db = get_db()
        rows = db.execute(
            "SELECT * FROM tracked_plays WHERE user_id = ? AND nivel = 'VALUE' "
            "ORDER BY fecha DESC, id DESC LIMIT ?",
            (VALUE_SYSTEM_USER_ID, n),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Calcula profit si no viene en la BD (formato W/L del tracker VALUE).
            # Sin esto fmt_money(None) tumba el Dashboard con 500 (2026-10-04).
            if d.get("profit") is None and d.get("resultado") in ("W", "WON", "L", "LOST"):
                try:
                    stake = float(d.get("stake_monto") or 50.0)
                    cuota = float(d.get("cuota") or 0)
                    if d["resultado"] in ("W", "WON"):
                        d["profit"] = stake * (cuota / 100.0) if cuota > 0 else stake * (100.0 / abs(cuota))
                    else:
                        d["profit"] = -stake
                except Exception:
                    d["profit"] = 0.0
            out.append(d)
        actuales, pasadas = [], []
        for d in out:
            pendiente = d.get("resultado") not in ("W", "WON", "L", "LOST")
            es_hoy = str(d.get("fecha") or "")[:10] == hoy
            (actuales if (pendiente or es_hoy) else pasadas).append(d)
        return {"actuales": actuales, "pasadas": pasadas, "todas": out}
    except Exception:
        return vacio


def official_tracker_full():
    """Tracker oficial COMPLETO para el Admin (2026-10-12, pedido por Alex:
    un tracker especial, más profesional y con más estadísticas que el del
    Dashboard de miembro). Todo calculado EN VIVO desde data/archive.json.
    Devuelve stats generales, por nivel, por banda de cuota, rachas,
    últimos 7/30 días, mejores/peores jugadas y las últimas 12 liquidadas."""
    from datetime import datetime as _dt, timedelta as _td
    dias = sorted(load_archive(), key=lambda d: d.get("fecha") or "")
    settled = []  # (fecha, jugada) en orden cronológico
    for d in dias:
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada"):
                settled.append((d.get("fecha") or "", j))

    def _agg(items):
        w = sum(1 for _, j in items if j["resultado"] == "WON")
        l = sum(1 for _, j in items if j["resultado"] == "LOST")
        profit = sum(float(j.get("profit") or 0.0) for _, j in items)
        risked = sum(float(j.get("stake_monto") or 0.0) for _, j in items)
        n = w + l
        _units = profit / 25.0  # 1u oficial = $25 (Platinum $25=1u, Gold $15=0.6u)
        return {"w": w, "l": l, "record": f"{w}–{l}",
                "win_rate": round(w / n * 100, 1) if n else 0.0,
                "profit": round(profit, 2),
                "units": round(_units, 2),
                "units_display": f"{_units:+.2f}u",
                "profit_display": (("+$" if profit > 0 else ("-$" if profit < 0 else "$"))
                                   + f"{abs(int(round(profit))):,}"),
                "cls": "pos" if profit > 0 else ("neg" if profit < 0 else ""),
                "risked": round(risked, 2),
                "roi": round(profit / risked * 100, 1) if risked else 0.0,
                "n": n}

    general = _agg(settled)
    cuotas = [int(j.get("cuota") or 0) for _, j in settled]
    general["avg_odds"] = round(sum(cuotas) / len(cuotas)) if cuotas else 0
    general["avg_stake"] = round(general["risked"] / general["n"], 2) if general["n"] else 0.0
    general["progress"] = general["n"]
    general["progress_pct"] = min(100.0, round(general["n"] / 200 * 100, 1))

    por_nivel = {}
    for nv in ("ELITE", "GOLD"):
        # PLATINUM = ELITE + las viejas con etiqueta PLATINUM del
        # pre-renombre (2026-10-15): asi Platinum + Gold = el total
        # oficial EXACTO y ningun libro deja jugadas fuera.
        _nvs = ("ELITE", "PLATINUM") if nv == "ELITE" else (nv,)
        por_nivel[nv] = _agg([it for it in settled
                              if (it[1].get("nivel") or "").upper() in _nvs])
    bandas = {
        "Favorites (−110 o mejor)": [it for it in settled if int(it[1].get("cuota") or 0) <= -110],
        "Pick'em (−109 a +109)": [it for it in settled if -109 <= int(it[1].get("cuota") or 0) <= 109],
        "Underdogs (+110 o más)": [it for it in settled if int(it[1].get("cuota") or 0) >= 110],
    }
    por_banda = {k: _agg(v) for k, v in bandas.items() if v}

    # Rachas (orden cronológico)
    racha_actual, mejor_w, peor_l = "—", 0, 0
    run_w = run_l = 0
    for _, j in settled:
        if j["resultado"] == "WON":
            run_w += 1; run_l = 0
        else:
            run_l += 1; run_w = 0
        mejor_w = max(mejor_w, run_w); peor_l = max(peor_l, run_l)
    if settled:
        racha_actual = f"W{run_w}" if run_w else f"L{run_l}"

    hoy = _dt.now().date()
    def _en_rango(it, dias_n):
        try:
            f = _dt.strptime(it[0], "%Y-%m-%d").date()
        except Exception:
            return False
        return (hoy - f).days < dias_n
    ult7 = _agg([it for it in settled if _en_rango(it, 7)])
    ult30 = _agg([it for it in settled if _en_rango(it, 30)])

    ordenadas = sorted(settled, key=lambda it: float(it[1].get("profit") or 0.0))
    # Share de victorias (2026-10-14): id estable por jugada + gate de
    # compartibilidad (ganada, con ticket y marcador parseable).
    _seen_pid = {}

    def _pid(f, j):
        pid = _archive_play_id(f, j, _seen_pid)
        _seen_pid[pid] = True
        return pid

    def _share_id(f, j):
        return _pid(f, j) if _share_data(f, j) else None

    peor = [{"fecha": f, "pick": j.get("pick"), "cuota": j.get("cuota_texto") or j.get("cuota"),
             "profit": float(j.get("profit") or 0.0), "nivel": (j.get("nivel") or "").upper(),
             "comprobante": j.get("comprobante"), "cierre": _cierre_txt(j), "clv": j.get("clv_pct"),
             "liga": _league_of_archive_play(j) or "",
             "abbr": j.get("abbr") or "", "opp_abbr": j.get("opp_abbr") or "",
             "unidades": round(float(j.get("profit") or 0.0) / 25.0, 2)} for f, j in ordenadas[:5]]
    mejor = [{"fecha": f, "pick": j.get("pick"), "cuota": j.get("cuota_texto") or j.get("cuota"),
              "profit": float(j.get("profit") or 0.0), "nivel": (j.get("nivel") or "").upper(),
              "comprobante": j.get("comprobante"), "cierre": _cierre_txt(j), "clv": j.get("clv_pct"),
              "liga": _league_of_archive_play(j) or "",
              "abbr": j.get("abbr") or "", "opp_abbr": j.get("opp_abbr") or "",
              "unidades": round(float(j.get("profit") or 0.0) / 25.0, 2),
              "share_id": _share_id(f, j)} for f, j in reversed(ordenadas[-5:])]
    ultimas = [{"fecha": f, "pick": j.get("pick"), "cuota": j.get("cuota_texto") or j.get("cuota"),
                "resultado": j.get("resultado"), "profit": float(j.get("profit") or 0.0),
                "nivel": (j.get("nivel") or "").upper(),
                "comprobante": j.get("comprobante"), "cierre": _cierre_txt(j), "clv": j.get("clv_pct"),
                "liga": _league_of_archive_play(j) or "",
                "abbr": j.get("abbr") or "", "opp_abbr": j.get("opp_abbr") or "",
                "unidades": round(float(j.get("profit") or 0.0) / 25.0, 2),
                "share_id": _share_id(f, j)} for f, j in reversed(settled[-12:])]
    # LIBRO COMPLETO (2026-10-15, pedido por Alex): TODAS las liquidadas,
    # mas nuevas primero, para la contabilidad por programa del tracker
    # (cada jugada con su informacion y su ticket al lado).
    historial = [{"fecha": f, "pick": j.get("pick"), "cuota": j.get("cuota_texto") or j.get("cuota"),
                  "resultado": j.get("resultado"), "profit": float(j.get("profit") or 0.0),
                  "nivel": (j.get("nivel") or "").upper(),
                  "comprobante": j.get("comprobante"), "cierre": _cierre_txt(j), "clv": j.get("clv_pct"),
                  "liga": _league_of_archive_play(j) or "",
                  "abbr": j.get("abbr") or "", "opp_abbr": j.get("opp_abbr") or "",
                  "unidades": round(float(j.get("profit") or 0.0) / 25.0, 2),
                  "share_id": _share_id(f, j)} for f, j in reversed(settled)]
    # Record por liga (2026-10-14, pedido por Alex) con PUERTA DE
    # RECONCILIACIÓN DURA: la sección solo existe si TODAS las jugadas
    # liquidadas tienen liga derivable (_league_of_archive_play: campo
    # `liga` del archivo o mapa equipo→liga sobre el pick) y las sumas por
    # liga cuadran EXACTAS con los totales oficiales (récord y profit).
    # Si algo no cuadra, por_liga = None y la sección no se muestra —
    # jamás se publica un desglose que no reconcilia.
    _LG_ICONS = {"NFL": "🏈", "NCAAF": "🎓", "MLB": "⚾", "NHL": "🏒",
                 "NBA": "🏀"}
    _lg_acc = {}
    _sin_liga = 0
    for _f, _j in settled:
        _L = _league_of_archive_play(_j)
        if not _L:
            _sin_liga += 1
            continue
        _s = _lg_acc.setdefault(_L, {"w": 0, "l": 0, "profit": 0.0,
                                     "risked": 0.0, "odds": []})
        if _j["resultado"] == "WON":
            _s["w"] += 1
        else:
            _s["l"] += 1
        _s["profit"] += float(_j.get("profit") or 0.0)
        _s["risked"] += float(_j.get("stake_monto") or 0.0)
        _s["odds"].append(int(_j.get("cuota") or 0))
    por_liga = None
    if not _sin_liga and _lg_acc:
        _tw = sum(s["w"] for s in _lg_acc.values())
        _tl = sum(s["l"] for s in _lg_acc.values())
        _tp = sum(s["profit"] for s in _lg_acc.values())
        if ((_tw, _tl, _tw + _tl) == (general["w"], general["l"],
                                      general["n"])
                and abs(_tp - general["profit"]) < 0.005):
            por_liga = []
            for _L in ("NFL", "NCAAF", "MLB", "NHL", "NBA"):
                if _L not in _lg_acc:
                    continue
                _s = _lg_acc[_L]
                _n = _s["w"] + _s["l"]
                _u = _s["profit"] / 25.0  # normalización oficial 1u = $25
                _avg = (round(sum(_s["odds"]) / len(_s["odds"]))
                        if _s["odds"] else 0)
                por_liga.append({
                    "liga": _L, "icon": _LG_ICONS.get(_L, "🎯"),
                    "w": _s["w"], "l": _s["l"], "n": _n,
                    "record": f"{_s['w']}–{_s['l']}",
                    "win_rate": round(_s["w"] / _n * 100, 1) if _n else 0.0,
                    "profit": round(_s["profit"], 2),
                    "profit_display": (
                        ("+$" if _s["profit"] > 0
                         else ("-$" if _s["profit"] < 0 else "$"))
                        + f"{abs(_s['profit']):,.2f}"),
                    "units": round(_u, 2),
                    "units_display": f"{_u:+.2f}u",
                    "cls": "pos" if _s["profit"] > 0 else (
                        "neg" if _s["profit"] < 0 else ""),
                    "roi": (round(_s["profit"] / _s["risked"] * 100, 1)
                            if _s["risked"] else 0.0),
                    "avg_odds": _avg,
                    "avg_odds_display": f"{_avg:+d}",
                })

    verif = sum(1 for _, j in settled if j.get("comprobante"))
    _clvs = [float(j["clv_pct"]) for _, j in settled if j.get("clv_pct") is not None]
    return {"general": general, "por_nivel": por_nivel, "por_banda": por_banda,
            "por_liga": por_liga,
            "racha_actual": racha_actual, "mejor_racha_w": mejor_w, "peor_racha_l": peor_l,
            "ult7": ult7, "ult30": ult30, "mejor": mejor, "peor": peor,
            "ultimas": ultimas, "historial": historial, "verificadas": verif,
            "verificadas_pct": round(verif / general["n"] * 100) if general["n"] else 0,
            "clv_n": len(_clvs),
            "clv_prom": round(sum(_clvs) / len(_clvs), 2) if _clvs else None}


def program_stats():
    """Stats del programa calculadas EN VIVO desde data/archive.json.

    Reemplaza a data/results.json (quedó obsoleto el 2026-09-27): el
    dashboard debe reflejar los resultados oficiales inmediatamente
    después de cada liquidación, sin archivos intermedios.
    """
    ganadas = perdidas = 0
    profit = risked = 0.0
    niveles = {"GOLD": {"ganadas": 0, "perdidas": 0, "profit": 0.0},
               "ELITE": {"ganadas": 0, "perdidas": 0, "profit": 0.0},
               "VALUE": {"ganadas": 0, "perdidas": 0, "profit": 0.0}}
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
    # VALUE del sistema: desde tracked_plays del usuario sistema (2026-10-04)
    # Las values se trackean automáticamente, las juegue Alex o no.
    try:
        _db = get_db()
        _vrows = _db.execute(
            "SELECT resultado, cuota, stake_monto FROM tracked_plays "
            "WHERE user_id = ? AND nivel = 'VALUE' AND resultado IN ('W', 'L')",
            (VALUE_SYSTEM_USER_ID,),
        ).fetchall()
        for _vr in _vrows:
            _vs = niveles["VALUE"]
            _cuota = int(_vr["cuota"] or 0)
            _stake = float(_vr["stake_monto"] or 50.0)
            if _vr["resultado"] == "W":
                _vs["ganadas"] += 1
                _vs["profit"] += _stake * (_cuota / 100.0) if _cuota > 0 else _stake * (100.0 / abs(_cuota))
            else:
                _vs["perdidas"] += 1
                _vs["profit"] -= _stake
    except Exception:
        pass
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
        "profit_units_display": f"{profit / 25.0:+.2f}u",
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


# Parlay Builder (Platinum tool): moneyline odds per game, sourced from
# sharp_board.json on the `data-board` branch — same feed as the /sharp page,
# so the builder burns ZERO Odds API credits per page view.
_parlay_games_cache = {"at": 0.0, "data": []}


def load_parlay_games():
    import time
    now = time.time()
    if now - _parlay_games_cache["at"] < 300 and _parlay_games_cache["data"]:
        return _parlay_games_cache["data"]
    games = []
    try:
        board = load_sharp_board()
        for g in board.get("games", []):
            line = g.get("line", {}) or {}
            away = (line.get("away") or {}).get("now")
            home = (line.get("home") or {}).get("now")
            if away is None or home is None:
                continue
            games.append({
                "id": g.get("id"),
                "sport": g.get("sport") or "",
                "game": g.get("game") or "",
                "away": g.get("away") or "",
                "home": g.get("home") or "",
                "away_odds": int(round(away)),
                "home_odds": int(round(home)),
                "kickoff": g.get("kickoff_et") or "",
            })
    except Exception:
        games = _parlay_games_cache["data"] or []
    _parlay_games_cache["at"] = now
    _parlay_games_cache["data"] = games
    return games


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
# La página /tools/line-moves lo lee en vivo (desde el 2026-10-19 es Platinum).
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


# Odds Board (Platinum): juegos de hoy con el mejor moneyline por lado.
# El JSON vive en la rama `data-board` (igual que line moves y el +EV
# Board); lo publica hidden_files/publish-odds-board.py con datos de
# The Odds API. Actualizar datos NO redespliega Render.
ODDS_BOARD_URL = (
    "https://raw.githubusercontent.com/penamanuel-art/tlb-members"
    "/data-board/data/odds_board.json"
)
_odds_board_cache = {"at": 0.0, "data": {}}


def load_odds_board():
    import time
    now = time.time()
    if now - _odds_board_cache["at"] < 300 and _odds_board_cache["data"]:
        return _odds_board_cache["data"]
    data = {}
    try:
        req = urllib.request.Request(ODDS_BOARD_URL, headers={"User-Agent": "tlb-members"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        data = _odds_board_cache["data"] or {}
    _odds_board_cache["at"] = now
    _odds_board_cache["data"] = data
    return data


def american_profit_ratio(odds) -> float:
    """Cuánto se gana por cada 1 apostado en cuota americana."""
    o = int(odds)
    if o == 0:
        return 0.0
    return o / 100.0 if o > 0 else 100.0 / abs(o)


def play_profit_units(tp) -> float:
    # float() obligatorio (2026-10-21): en Postgres los montos pueden llegar
    # como Decimal y Decimal * float lanza TypeError (500 en /home con
    # sesión; en SQLite llegan float y el mismo código no fallaba).
    ratio = american_profit_ratio(tp["cuota"])
    if tp["resultado"] == "W":
        return float(tp["stake_unidades"]) * ratio
    if tp["resultado"] == "L":
        return -float(tp["stake_unidades"])
    return 0.0


def play_profit_dollars(tp) -> float:
    # float() obligatorio: misma divergencia Decimal de Postgres que en
    # play_profit_units (2026-10-21).
    ratio = american_profit_ratio(tp["cuota"])
    if tp["resultado"] == "W":
        return float(tp["stake_monto"]) * ratio
    if tp["resultado"] == "L":
        return -float(tp["stake_monto"])
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
    risked = sum(float(t["stake_monto"] or 0) for t in graded)
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


def get_setting(key, default=None):
    """Lee un ajuste global de site_settings (None si no existe)."""
    try:
        row = get_db().execute(
            "SELECT value FROM site_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key, value):
    db = get_db()
    if USE_PG:
        db.execute(
            "INSERT INTO site_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    else:
        db.execute(
            "INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    db.commit()


def home_lux_enabled():
    """Diseño premium (lux) del home: activo salvo que el admin lo revierta
    con el botón de revertir (2026-10-12, pedido por Alex)."""
    return get_setting("home_lux", "1") == "1"


def program_curve():
    """Curva SVG del profit acumulado REAL del programa (del archivo)."""
    try:
        vals, cum = [], 0.0
        for d in sorted(load_archive(), key=lambda x: x.get("fecha", "")):
            for j in d.get("jugadas", []) or []:
                if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada"):
                    cum += float(j.get("profit") or 0)
                    vals.append(round(cum, 2))
        return profit_curve_svg(vals, w=320, h=90, pad=8)
    except Exception:
        return None


def stats_por_nivel(tracked):
    """Ganadas/perdidas/profit por nivel (GOLD/ELITE/VALUE) para el bloque estilo WGT."""
    out = {}
    for t in tracked:
        lvl = (t.get("nivel") or "").upper()
        if lvl not in ("GOLD", "ELITE", "VALUE"):
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
    for _lvl in ("GOLD", "ELITE", "VALUE"):
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


def _ensure_book_balances_table(db):
    """Crea la tabla de balances por casa si no existe (fuera del DDL inicial
    para no romper el deploy)."""
    if db.use_pg:
        db.execute(
            """CREATE TABLE IF NOT EXISTS book_balances (
                   id SERIAL PRIMARY KEY,
                   user_id INTEGER NOT NULL REFERENCES users(id),
                   book_name TEXT NOT NULL,
                   balance DOUBLE PRECISION NOT NULL DEFAULT 0,
                   updated_at TEXT NOT NULL,
                   UNIQUE(user_id, book_name)
               )"""
        )
    else:
        db.execute(
            """CREATE TABLE IF NOT EXISTS book_balances (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL REFERENCES users(id),
                   book_name TEXT NOT NULL,
                   balance REAL NOT NULL DEFAULT 0,
                   updated_at TEXT NOT NULL,
                   UNIQUE(user_id, book_name)
               )"""
        )
    db.commit()


def bump_book_balance(db, user_id, book_name, delta):
    """Suma delta (+/-) al balance de una casa de apuestas.

    Se llama al liquidarse una jugada trackeada: la ganancia o pérdida va a
    la casa del ticket. (Pedido por Alex 2026-10-05: los balances se
    actualizan solos al liquidarse, ya no son solo manuales.)
    """
    try:
        delta = round(float(delta or 0), 2)
    except (TypeError, ValueError):
        return
    if not delta or not book_name:
        return
    _ensure_book_balances_table(db)
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT balance FROM book_balances WHERE user_id = ? AND book_name = ?",
        (user_id, book_name),
    ).fetchone()
    if row:
        try:
            nuevo = round(float(row["balance"] or 0) + delta, 2)
        except (TypeError, ValueError):
            nuevo = delta
        db.execute(
            "UPDATE book_balances SET balance = ?, updated_at = ? WHERE user_id = ? AND book_name = ?",
            (nuevo, now, user_id, book_name),
        )
    else:
        db.execute(
            "INSERT INTO book_balances (user_id, book_name, balance, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, book_name, delta, now),
        )
    db.commit()


def auto_grado_tracked(db, user):
    """Liquida automáticamente las jugadas trackeadas pendientes usando los
    resultados oficiales del programa (data/archive.json).

    Para el administrador y para la cuenta personal de miembro de Alex: su
    tracker se actualiza solo en cuanto la liquidación nocturna publica el
    resultado oficial. Los demás miembros marcan sus resultados a mano,
    según sus propias jugadas.

    Se ejecuta en cada vista del tracker y del home.

    2026-10-06 (pedido por Alex: su tracker personal lleva los mismos
    números que el récord oficial del programa): para su cuenta de miembro,
    cada jugada trackeada que exista en el archivo oficial se sincroniza
    por play_id — resultado, cuota y montos reales del ticket — así el W-L
    y el profit quedan idénticos al programa. El match por play_id es
    determinista; el match por (fecha, pick) queda como respaldo para
    pendientes sin play_id. Nunca se reescribe un resultado ya marcado a
    mano; solo se completa el pendiente (NULL).
    """
    if not (is_admin_for(user) or is_alex_member(user)):
        return 0
    user_id = user["id"]
    # Los balances viven en la cuenta personal de Alex (igual que en /balances)
    if is_admin_for(user) and not is_alex_member(user):
        _r = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
        ).fetchone()
        balance_uid = _r["id"] if _r else user_id
    else:
        balance_uid = user_id
    solo_alex = is_alex_member(user)
    # Índice oficial: por play_id (determinista) y por (fecha, pick) como respaldo.
    oficiales_by_id = {}
    oficiales_by_key = {}
    for d in load_archive():
        fecha = (d.get("fecha") or "").strip()
        for j in d.get("jugadas", []) or []:
            r = j.get("resultado")
            if r not in ("WON", "LOST"):
                continue
            res = "W" if r == "WON" else "L"
            pid = (j.get("id") or "").strip()
            if pid:
                oficiales_by_id[pid] = (j, res, fecha)
            # Respaldo (2026-10-03): el pick trackeado viene de la card
            # ("TCU ML (+186)") y el oficial del archivo ("TCU ML vs BYU");
            # se comparan sin cuota ni rival.
            pick = _norm_free_pick(j.get("pick") or "").lower()
            if pick:
                oficiales_by_key[(fecha, pick)] = (j, res, fecha)
    rows = db.execute(
        "SELECT id, play_id, fecha, pick, cuota, stake_monto, stake_unidades, "
        "resultado FROM tracked_plays WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    if not rows:
        return 0
    n = 0
    for t in rows:
        tid = t["id"]
        pid = (t["play_id"] or "").strip()
        official = oficiales_by_id.get(pid) if pid else None
        if not official and not t["resultado"]:
            f = (t["fecha"] or "").strip() or (
                _RE_FECHA_PLAYID.match(pid).group(1)
                if pid and _RE_FECHA_PLAYID.match(pid) else ""
            )
            official = oficiales_by_key.get(
                (f, _norm_free_pick(t["pick"] or "").lower())
            )
        if not official:
            continue
        j, res, fecha_of = official
        era_pendiente = not t["resultado"]
        updates = {}
        # Resultado: solo se completa el pendiente; nunca se reescribe uno marcado.
        if era_pendiente:
            updates["resultado"] = res
        # Sincronía con el programa (solo la cuenta de Alex): cuota y montos
        # reales del ticket para que el profit sea idéntico al oficial.
        if solo_alex:
            try:
                cuota_of = int(j.get("cuota") or 0)
            except (TypeError, ValueError):
                cuota_of = 0
            if cuota_of and cuota_of != (t["cuota"] or 0):
                updates["cuota"] = cuota_of
            try:
                monto_of = float(j.get("stake_monto") or 0)
            except (TypeError, ValueError):
                monto_of = 0
            if monto_of > 0 and abs(monto_of - float(t["stake_monto"] or 0)) > 0.005:
                updates["stake_monto"] = monto_of
            try:
                u_of = float(j.get("stake_unidades") or 0)
            except (TypeError, ValueError):
                u_of = 0
            if u_of > 0 and abs(u_of - float(t["stake_unidades"] or 0)) > 0.001:
                updates["stake_unidades"] = u_of
            if fecha_of and fecha_of != (t["fecha"] or "").strip():
                updates["fecha"] = fecha_of
        if not updates:
            continue
        set_clause = ", ".join("%s = ?" % k for k in updates)
        db.execute(
            "UPDATE tracked_plays SET %s WHERE id = ?" % set_clause,
            (*updates.values(), tid),
        )
        # Balance automático de la casa (2026-10-05, pedido por Alex):
        # todo lo de Alex se apuesta en Novig; la ganancia/pérdida de la
        # jugada actualiza ese balance en cuanto se liquida (solo al pasar
        # de pendiente a W/L, con los montos ya sincronizados; no se repite).
        if era_pendiente:
            try:
                profit = play_profit_dollars(
                    {
                        "cuota": updates.get("cuota", t["cuota"]) or 0,
                        "resultado": res,
                        "stake_monto": float(
                            updates.get("stake_monto", t["stake_monto"]) or 0
                        ),
                    }
                )
            except (TypeError, ValueError):
                profit = 0
            if profit:
                bump_book_balance(db, balance_uid, "Novig", profit)
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
    del tracker Gold — la misma jugada registrada con dos nombres
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


def _free_stake(nivel, stake_unidades):
    """Stake simulado del tracker Gold: 1% de FREE_TEST_BANKROLL ($10k).

    Orden de Alex 2026-10-03: mismo método WGT — 1u = 1% ($100), 0.6u = $60.
    """
    try:
        u = float(stake_unidades or 0)
    except (TypeError, ValueError):
        u = 0
    if u <= 0:
        u = 1.0 if str(nivel or "").upper() == "ELITE" else 0.6
    return u, round(FREE_TEST_BANKROLL * 0.01 * u, 2)


def sync_free_plays(db):
    """Registra automáticamente en el tracker "Gold" las jugadas de la
    card publicada (data/plays.json) y del histórico (data/archive.json)
    que aún no estén registradas.

    Cada jugada entra con stake simulado 1% de $10,000 por unidades
    (2026-10-03, orden de Alex). Idempotente: las jugadas
    ya registradas (por play_id) se saltan. Se llama al publicar la card y
    al abrir /free-plays.
    """
    _dedup_free_plays(db)  # 2026-10-01: limpia duplicados existentes (una fila por juego)
    now = datetime.now(timezone.utc).isoformat()
    n = 0

    def _insert(pid, fecha, nivel, pick, cuota, edge, stake_unidades=None):
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
        u, monto = _free_stake(nivel, stake_unidades)
        db.execute(
            "INSERT INTO free_plays "
            "(play_id, fecha, nivel, pick, cuota, stake_unidades, stake_monto, edge, resultado, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (pid, fecha or "", nivel or "GOLD", pick or "", cuota,
             u, monto, edge, now),
        )
        n += 1

    # 1) Card actual (plays.json)
    for p in load_plays():
        _insert(
            p.get("id"), p.get("fecha"), p.get("nivel"),
            p.get("pick"), p.get("cuota"), p.get("edge"),
            p.get("stake_unidades"),
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
                j.get("stake_unidades"),
            )

    if n:
        db.commit()
    return n


def auto_grado_free(db):
    """Liquida las jugadas pendientes del tracker "Gold" usando los
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
            # Matching normalizado igual que auto_grado_tracked (2026-10-03).
            pick = _norm_free_pick(j.get("pick") or "").lower()
            if r in ("WON", "LOST") and pick:
                oficiales[(fecha, pick)] = "W" if r == "WON" else "L"
    n = 0
    for t in pendientes:
        f = (t["fecha"] or "").strip() or (
            _RE_FECHA_PLAYID.match(t["play_id"] or "").group(1)
            if _RE_FECHA_PLAYID.match(t["play_id"] or "") else ""
        )
        key = (f, _norm_free_pick(t["pick"] or "").lower())
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
    try:
        o = int(str(o).replace("−", "-").strip())
    except (TypeError, ValueError):
        return "—"
    return f"+{o}" if o > 0 else str(o)


app.jinja_env.globals.update(fmt_money=fmt_money, fmt_units=fmt_units, fmt_odds=fmt_odds)


def nivel_display(nivel) -> str:
    """Nombre VISIBLE de un nivel (2026-10-14, bug visto por Alex): el
    valor interno 'ELITE' se muestra SIEMPRE como 'PLATINUM' (renombre del
    2026-10-06). Solo capa de presentación — JSON, BD, crons y scripts
    siguen usando ELITE/GOLD como valores internos."""
    n = (nivel or "").upper()
    return "PLATINUM" if n in ("ELITE", "PLATINUM") else n


app.jinja_env.globals.update(nivel_display=nivel_display)


def _load_ncaaf_abbr2id():
    # ESPN exige ID numérico para logos NCAAF (ncaa/500/<id>.png); NFL/MLB aceptan abreviatura.
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ncaaf-abbr2id.json")) as f:
            return {k.upper(): v for k, v in json.load(f).items()}
    except Exception:
        return {}


NCAAF_ABBR2ID = _load_ncaaf_abbr2id()


def team_logo_url(liga, abbr) -> str:
    """URL del logo ESPN del equipo apostado (orden Alex 2026-10-03: siempre en la tarjeta)."""
    if not abbr:
        return ""
    a = str(abbr).strip().upper()
    lg = str(liga or "").strip().upper()
    if lg == "NFL":
        return f"https://a.espncdn.com/i/teamlogos/nfl/500/{a}.png"
    if lg == "MLB":
        return f"https://a.espncdn.com/i/teamlogos/mlb/500/{a}.png"
    if lg in ("NCAAF", "NCAAB"):
        tid = NCAAF_ABBR2ID.get(a)
        if tid:
            return f"https://a.espncdn.com/i/teamlogos/ncaa/500/{tid}.png"
    if lg == "NHL":
        return f"https://a.espncdn.com/i/teamlogos/nhl/500/{a.lower()}.png"
    return ""


app.jinja_env.globals.update(team_logo_url=team_logo_url)

# Marcas de agua rotativas (2026-09-30, pedido por Alex): cada sesión ve una
# distinta — fútbol americano o béisbol — siempre tenue.
WATERMARKS = [
    "img/watermark-football.jpg",
    "img/watermark-baseball.jpg",
    "img/watermark-hockey.jpg",
]

# Fondo por sección (2026-10-06, pedido por Alex): cada página muestra
# el fondo de su deporte/tema correspondiente.
# Fondos rotativos: TRES (2026-10-15, orden de Alex) — futbol
# americano, beisbol y NHL, los tres deportes del programa.
# Sin fondo fijo por ruta: rotan por sesion entre los tres.
WATERMARK_POR_RUTA = {}


def _seguro(fn, defecto):
    """Context processor a prueba de fallos (2026-10-21): un cálculo
    decorativo (racha, ticker) NUNCA puede tumbar el render de una
    página; ante cualquier excepción devuelve el valor neutro."""
    try:
        return fn()
    except Exception:
        return defecto


@app.context_processor
def inject_user():
    from flask import request as freq
    ruta = (freq.endpoint or "")
    wm = WATERMARK_POR_RUTA.get(ruta)
    if wm is None:
        # SIN marca de agua (2026-10-16, orden de Alex: "quítale esos
        # fondos de futbolista, ponle un fondo normal"): ya no rotan las
        # fotos de jugadores; la página usa su fondo normal.
        wm = ""
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
    etiqueta = "Platinum Member" if platinum_unlocked_for(cu) else "Member"
    try:
        past_due = bool(cu and cu["stripe_past_due"])
    except (KeyError, IndexError, TypeError):
        past_due = False
    try:
        cuenta_es = bool(cu) and (cu["lang"] or "") == "es"
    except (KeyError, IndexError, TypeError):
        cuenta_es = False
    trial_dias_left = None
    try:
        if cu and not (cu["stripe_subscription_id"] or ""):
            te = cu["platinum_trial_ends"]
            if te:
                resto = (datetime.fromisoformat(te) - datetime.now()).total_seconds()
                if resto > 0:
                    trial_dias_left = max(1, int(resto // 86400) + (1 if resto % 86400 > 0 else 0))
    except (KeyError, IndexError, TypeError, ValueError):
        trial_dias_left = None
    if trial_dias_left is None and cu and platinum_unlocked_for(cu):
        # Vista previa de Alex (2026-10-17): su cuenta de miembro es pase de
        # dueno sin reloj; su burbuja muestra los dias del grupo del mes
        # gratis (vence 2026-11-15) para que vea el diseno como un miembro.
        try:
            if (cu["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL:
                resto = (datetime(2026, 11, 15, 23, 59) - datetime.now()).total_seconds()
                if resto > 0:
                    trial_dias_left = max(1, int(resto // 86400) + (1 if resto % 86400 > 0 else 0))
        except (KeyError, IndexError, TypeError):
            pass
    return {
        "nombre_corto": corto,
        "nombre_completo": nombre,
        "inicial": corto[:1].upper(),
        "foto_perfil": foto,
        "dias_miembro": dias_miembro,
        "etiqueta_miembro": etiqueta,
        "es_admin": bool(session.get("is_admin")),
        "cuenta_es": cuenta_es,
        "is_alex": bool(cu and (cu["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL),
        "miembro_platinum": platinum_unlocked_for(cu),
        "trial_dias_left": trial_dias_left,
        "past_due": past_due,
        "ticker_days": _seguro(compute_ticker_days, []),
        "racha_dias": _seguro(win_streak_days, 0),
        "asset_v": ASSET_V,
        "watermark_img": wm,
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
        for _col, _default in [("notif_plays", "1"), ("notif_graded", "1"), ("notif_live", "1"), ("notif_reminder", "1"), ("notif_ev", "1"), ("notif_price", "0"), ("notif_sharp", "1"), ("notif_masterclass", "1")]:
            if _col not in cols:
                pending.append(f"ALTER TABLE users ADD COLUMN {_col} INTEGER NOT NULL DEFAULT {_default}")
        if "is_admin" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if "cancel_requested_at" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN cancel_requested_at TEXT")
        if "stripe_customer_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        if "stripe_subscription_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        if "stripe_past_due" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stripe_past_due INTEGER NOT NULL DEFAULT 0")
        if "platinum_trial_ends" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN platinum_trial_ends TEXT")
        if "foto" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN foto TEXT")
        if "telegram_user_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_user_id TEXT")
        if "telegram_ban_pending" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_ban_pending INTEGER NOT NULL DEFAULT 0")
        if "telegram_unban_pending" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_unban_pending INTEGER NOT NULL DEFAULT 0")
        if "phone" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN phone TEXT")
        if "sms_optin" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN sms_optin INTEGER NOT NULL DEFAULT 0")
        if "welcome_email_sent" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN welcome_email_sent INTEGER NOT NULL DEFAULT 0")
        if "lang" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN lang TEXT")
        if "reset_token" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN reset_token TEXT")
        if "reset_expires" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN reset_expires TEXT")
        # Activación por correo (2026-10-18, orden de Alex): los miembros que
        # ya existen quedan verificados (DEFAULT 1) — nadie pierde acceso;
        # solo los registros NUEVOS nacen sin activar (email_verified = 0).
        if "email_verified" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
        if "activation_token" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN activation_token TEXT")
        # Sin verificar no es miembro (2026-10-19, orden de Alex): lo que
        # manda es la ACTIVACIÓN — fecha en que confirmó su correo. Los ya
        # verificados heredan su fecha de registro como fecha de activación.
        if "activated_at" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN activated_at TEXT")
            pending.append("UPDATE users SET activated_at = created_at WHERE email_verified = 1 AND activated_at IS NULL")
        if "activation_reminders" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN activation_reminders INTEGER NOT NULL DEFAULT 0")
        if "activation_reminder_last" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN activation_reminder_last TEXT")
        # Página Account estilo WGT (2026-10-12, pedido por Alex): dirección
        # postal del perfil + perfil de apuestas (estado y casas).
        for _col in ["addr_line1", "addr_line2", "addr_city", "addr_state",
                     "addr_zip", "addr_country", "bet_state", "bet_books"]:
            if _col not in cols:
                pending.append(f"ALTER TABLE users ADD COLUMN {_col} TEXT")
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
        # Columnas juego/liga en tracked_plays (2026-10-10: para el cron
        # value-results-update que liquida VALUE plays al terminar cada juego).
        if USE_PG:
            tcols = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'tracked_plays'"
            ).fetchall()}
        else:
            tcols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_plays)").fetchall()}
        if tcols:
            if "juego" not in tcols:
                conn.execute("ALTER TABLE tracked_plays ADD COLUMN juego TEXT")
            if "liga" not in tcols:
                conn.execute("ALTER TABLE tracked_plays ADD COLUMN liga TEXT")
            conn.commit()
        # Códigos de vinculación Telegram (un solo uso, los crea /cuenta).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            )"""
        )
        # Botón de disparo de publicación (2026-10-12, pedido por Alex):
        # el Admin registra aquí las jugadas aprobadas y el agente las
        # publica de verdad en el Dashboard (commit/push verificado).
        _pr_id = "id SERIAL PRIMARY KEY" if USE_PG else "id INTEGER PRIMARY KEY AUTOINCREMENT"
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS publish_requests (
                {_pr_id},
                fecha TEXT NOT NULL,
                play_ids TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                done_at TEXT
            )"""
        )
        # Ajustes globales del sitio (2026-10-12): pares clave/valor para
        # interruptores como el diseño del home (lux on/off con botón de
        # revertir del admin, pedido por Alex).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        # Pool de links de invitación a Sharp Club (un solo uso, los genera
        # el cron de la VM con el token del bot; /cuenta los reparte).
        # 2026-10-02: EN DESUSO — reemplazado por el link permanente
        # TELEGRAM_VIP_INVITE_LINK. La tabla queda como respaldo.
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
        # Clicks en links rastreados (pedido Alex 2026-10-02: notificación
        # instantánea por Telegram cuando alguien toca el link del anuncio).
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS link_clicks (
                {_id_col},
                slug TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ip TEXT NOT NULL DEFAULT '',
                ua TEXT NOT NULL DEFAULT '',
                referer TEXT NOT NULL DEFAULT ''
            )"""
        )
        conn.commit()
    finally:
        conn.close()


migrate_db()  # no borra datos: solo agrega la columna si falta


# ------------------------------------------------------------ Routes ----
def _public_stats():
    """Récord/ROI/total en vivo desde data/archive.json (compartido por /free y /)."""
    wins = losses = 0
    profit = staked = 0.0
    try:
        with open(os.path.join(os.path.dirname(__file__), "data", "archive.json")) as f:
            dias = json.load(f).get("dias", [])
        for dia in dias:
            for j in dia.get("jugadas", []):
                r = j.get("resultado")
                if r == "WON":
                    wins += 1
                elif r == "LOST":
                    losses += 1
                else:
                    continue
                profit += float(j.get("profit") or 0)
                staked += float(j.get("stake_monto") or 0)
    except Exception:
        pass
    total = wins + losses
    roi = (100.0 * profit / staked) if staked else 0.0
    return wins, losses, total, roi


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("home"))
    wins, losses, total, roi = _public_stats()
    return render_template("index.html", wins=wins, losses=losses, total=total, roi=roi)


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/legal")
def legal_page():
    return render_template("legal.html")


@app.route("/contact")
def contact_page():
    return render_template("contact.html")


@app.route("/free")
def free_landing():
    """Landing pública estilo WGT para la membresía gratuita (pedido por Alex 2026-10-03).

    OJO: /free-plays es el tracker admin de jugadas simuladas — esta página usa /free.
    Stats en vivo desde data/archive.json (verificado 2026-10-03: 15-5, +52.08% ROI, 20 jugadas).
    """
    wins, losses, total, roi = _public_stats()
    return render_template(
        "free_landing.html",
        wins=wins,
        losses=losses,
        total=total,
        roi=roi,
        stripe_url=url_for("register"),
    )


def compute_ticker_days():
    """Ticker de resultados (pedido por Alex 2026-09-26; CORREGIDO el
    2026-10-17 por orden de Alex: la franja dice YESTERDAY y debe ser el
    dia calendario ANTERIOR siempre — antes tomaba el ultimo dia con
    liquidadas y se quedaba pegada en un dia viejo cuando ayer no tuvo
    resultados, p. ej. un juego pospuesto). En vivo desde
    data/archive.json. Si ayer no tuvo jugadas liquidadas, la franja lo
    dice ("NO SETTLED PLAYS") en vez de saltar a un dia mas viejo."""
    ayer = datetime.now(TZ).date() - timedelta(days=1)
    ayer_iso = ayer.isoformat()
    etiqueta = ayer.strftime("%a %b %d").upper()
    dia = None
    for d in load_archive():
        if d.get("fecha") == ayer_iso:
            dia = d
            break
    jugadas = []
    if dia is not None:
        jugadas = [
            j for j in (dia.get("jugadas", []) or [])
            if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada")
        ]
    if not jugadas:
        return [{
            "etiqueta": etiqueta,
            "record": "NO SETTLED PLAYS",
            "profit": 0.0,
            "roi": 0.0,
        }]
    wins = sum(1 for j in jugadas if j.get("resultado") == "WON")
    losses = len(jugadas) - wins
    profit = sum(float(j.get("profit") or 0.0) for j in jugadas)
    risked = sum(float(j.get("stake_monto") or 0.0) for j in jugadas)
    roi = (profit / risked * 100.0) if risked else 0.0
    return [{
        "etiqueta": etiqueta,
        "record": f"{wins}-{losses}",
        "profit": profit,
        "roi": roi,
    }]


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
    # Fila de compartir (estilo referencia WGT, 2026-10-17): por ahora
    # SOLO la ve Alex (admin o su cuenta de miembro) hasta que apruebe
    # publicarla para todos.
    return render_template(
        "ticket.html", p=play, ver_compartir=_admin_o_cuenta_alex(current_user())
    )


def last_elite_win():
    """Última jugada ELITE ganada en el archivo (prueba social de la tarjeta bloqueada)."""
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:
        return None
    for d in sorted(arch.get("dias", []), key=lambda x: x.get("fecha", ""), reverse=True):
        for j in d.get("jugadas", []):
            if j.get("nivel") == "ELITE" and j.get("resultado") == "WON" and j.get("profit") and not j.get("bloqueada"):
                return int(round(j["profit"]))
    return None


def platinum_reciente_con_ticket():
    """(jugada, fecha) de la Platinum (ELITE) más reciente con comprobante
    en el archivo, o (None, None) si no hay ninguna.

    Fuente de la VISTA PREVIA admin de la carta bloqueada
    (?preview=locked) cuando aún no hay card de hoy publicada: Alex
    juzga la carta con un ticket real — hoy, el de Tampa Bay Rays ML
    del 2026-10-19 (2026-10-20: «hazlo con la Platinum de Tampa, usa
    ese recibo»). El flujo de miembros NO usa esta función.
    """
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:
        return None, None
    for d in sorted(arch.get("dias", []), key=lambda x: x.get("fecha", ""), reverse=True):
        for j in d.get("jugadas", []):
            if j.get("nivel") == "ELITE" and (j.get("comprobante") or "").strip():
                return j, d.get("fecha")
    return None, None


def locked_ticket_url_for(play, fecha):
    """URL pública de la copia DIFUMINADA del ticket Platinum de hoy.

    Doctrina sellada (fuga real del 2026-10-15): el difuminado va HORNEADO
    en el archivo — pixelado fuerte (//22) + GaussianBlur(16), los mismos
    parámetros del mecanismo del email free v2 — con nombre neutro
    img/locked/locked-<fecha>.jpg. La carta bloqueada del home JAMÁS
    sirve la foto original ni deja su nombre en el HTML. La copia se
    genera una vez por fecha dentro de static/ y se reutiliza; si ya
    existe (p. ej. la dejó el flujo del email), se usa tal cual. Falla
    cerrado: ante cualquier problema devuelve None y la carta sale sin
    foto, con su panel bloqueado.
    """
    try:
        comp = ((play or {}).get("comprobante") or "").strip()
        if not comp or not fecha:
            return None
        # El comprobante debe ser una foto nuestra bajo static, sin escapes.
        if ".." in comp or comp.startswith(("/", "http")) \
                or not comp.startswith("img/comprobantes/"):
            return None
        rel = f"img/locked/locked-{fecha}.jpg"
        dest = os.path.join(app.static_folder, rel)
        if not os.path.exists(dest):
            src = os.path.join(app.static_folder, comp)
            if not os.path.exists(src):
                return None
            from PIL import Image, ImageFilter
            im = Image.open(src).convert("RGB")
            w, h = im.size
            im = im.resize((max(20, w // 22), max(20, h // 22)), Image.BOX)
            im = im.resize((w, h), Image.BILINEAR)
            im = im.filter(ImageFilter.GaussianBlur(16))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            im.save(dest, quality=82)
        return url_for("static", filename=rel)
    except Exception:
        return None


def _locked_play_view(play, ticket_url):
    """Vista SANITIZADA de una jugada Platinum para la carta bloqueada.

    La carta bloqueada del home (estados sellados por Alex, 2026-10-20)
    es LA MISMA carta original en estado bloqueado: solo viajan liga,
    hora, matchup y la URL del ticket difuminado. El pick, la cuota, el
    stake, el edge, el write-up y la ruta del comprobante original NO
    entran en este dict — así jamás pueden aparecer en el HTML de quien
    no tiene Platinum, ni como texto, ni en atributos, ni en URLs.
    """
    play = play or {}
    return {
        "liga": play.get("liga") or "",
        "game_time_et": play.get("game_time_et") or "",
        "hora": play.get("hora") or "",
        "team_name": play.get("team_name") or "",
        "abbr": play.get("abbr") or "",
        "opp_name": play.get("opp_name") or "",
        "opp_abbr": play.get("opp_abbr") or "",
        "ticket_url": ticket_url,
    }


@app.route("/theme/<name>")
@login_required
def theme(name):
    # Selector de tema del Dashboard (pedido Alex 2026-10-02): v2 = look moderno,
    # v1 = diseño actual. El cambio es por sesión y reversible en un toque.
    if name in ("v1", "v2"):
        session["theme"] = name
    return redirect(request.args.get("next") or url_for("home"))


@app.route("/admin/diseno-home", methods=["POST"])
@login_required
def admin_diseno_home():
    """Interruptor del diseño premium del home (2026-10-12, pedido por Alex):
    el botón de revertir apaga el diseño lux para TODOS y vuelve al home
    anterior; el mismo botón lo vuelve a encender. Solo admin."""
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("home"))
    nuevo = "0" if home_lux_enabled() else "1"
    set_setting("home_lux", nuevo)
    if nuevo == "1":
        flash("New design is ON.", "ok")
    else:
        flash("Design reverted — previous home is live.", "ok")
    return redirect(url_for("home"))


def hot_run_cards(value_rows):
    """Franja «Running hot» del Home (2026-10-22, pedido por Alex):
    tarjetas de rachas calculadas EN VIVO desde las jugadas liquidadas
    (archivo oficial + tracker VALUE del sistema). Solo salen rachas
    reales que califican: últimas 10 con 7+ ganadas, últimas 5 con 4+
    ganadas, o racha ganadora vigente de 2+. Si ninguna califica, la
    sección no se muestra — nunca relleno."""
    def _card(badge, tag, seq, scope):
        if not seq:
            return None
        streak = 0
        for r in reversed(seq):
            if r == "W":
                streak += 1
            else:
                break
        n = len(seq)
        if n >= 10:
            w = seq[-10:].count("W")
            if w >= 7:
                return {"badge": badge, "tag": tag, "big": f"{w}\u2013{10 - w}",
                        "desc": f"Last 10 {scope} plays",
                        "dots": [r == "W" for r in seq[-10:]]}
        if n >= 5:
            w = seq[-5:].count("W")
            if w >= 4:
                extra = f" \u00b7 won the last {streak}" if streak >= 3 else ""
                return {"badge": badge, "tag": tag, "big": f"{w}\u2013{5 - w}",
                        "desc": f"Last 5 {scope} plays{extra}",
                        "dots": [r == "W" for r in seq[-5:]]}
        if streak >= 2:
            return {"badge": badge, "tag": tag, "big": f"{streak}\u20130",
                    "desc": f"Won the last {streak} {scope} plays",
                    "dots": [True] * streak}
        return None

    settled = []
    for d in sorted(load_archive(), key=lambda x: x.get("fecha") or ""):
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada"):
                settled.append(j)

    def seq_of(filtro):
        return ["W" if j["resultado"] == "WON" else "L"
                for j in settled if filtro(j)]

    cards = []
    for badge, tag, seq, scope in (
        ("OFFICIAL PLAYS", "ALL", seq_of(lambda j: True), "official"),
        ("PLATINUM", "ALL",
         seq_of(lambda j: (j.get("nivel") or "").upper() in ("ELITE", "PLATINUM")),
         "Platinum"),
        ("GOLD", "ALL",
         seq_of(lambda j: (j.get("nivel") or "").upper() == "GOLD"), "Gold"),
    ):
        c = _card(badge, tag, seq, scope)
        if c:
            cards.append(c)
    vseq = ["W" if r.get("resultado") == "W" else "L"
            for r in sorted(value_rows,
                            key=lambda r: (r.get("fecha") or "", r.get("id") or 0))
            if r.get("resultado") in ("W", "L")]
    c = _card("VALUE", "ALL", vseq, "value")
    if c:
        cards.append(c)
    ligas = {}
    for j in settled:
        lg = _league_of_archive_play(j) or ""
        if lg:
            ligas.setdefault(lg, []).append(
                "W" if j["resultado"] == "WON" else "L")
    league_cards = []
    for lg, s in ligas.items():
        c = _card("OFFICIAL PLAYS", lg, s, lg)
        if c:
            league_cards.append(c)
    league_cards.sort(key=lambda c: -len(c["dots"]))
    cards.extend(league_cards)
    return cards[:6]


@app.route("/home")
@login_required
def home():
    db = get_db()
    record_checkin(db, session["user_id"])
    _registrar_visita(current_user())
    try:
        _notify_login_telegram(current_user())  # 2026-10-05: aviso a Alex por Telegram en cada visita
    except Exception:
        pass
    auto_grado_tracked(db, current_user())
    program, plays = load_data()
    user = current_user()
    _card_publicada = card_publicada_hoy()
    card_pendiente = not _card_publicada or not plays
    if card_pendiente:
        # Solo el administrador ve la card pendiente (2026-10-03, orden de Alex:
        # su cuenta personal de miembro es un miembro normal más y solo ve
        # jugadas publicadas; él opera desde el dashboard de admin).
        pending = load_pending_plays() if is_admin_for(user) else []
        if pending:
            plays = pending
            card_pendiente = False
        else:
            plays = []  # las de ayer no se muestran: la card de hoy aún no sale
    plays = personalizar_plays(plays, user)
    tracked_rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ?",
        (session["user_id"],),
    ).fetchall()
    # VALUE del sistema: se muestran en la sección VALUE para todos
    # (2026-10-04, orden de Alex: "las valúes se trakean las juegue yo o no")
    system_value_rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? AND nivel = 'VALUE'",
        (VALUE_SYSTEM_USER_ID,),
    ).fetchall()
    try:
        hot_cards = hot_run_cards([dict(r) for r in system_value_rows])
    except Exception:
        hot_cards = []
    tracked_ids = {r["play_id"] for r in tracked_rows}
    tstats = compute_stats([dict(r) for r in tracked_rows])
    # tlevels incluye VALUE del sistema para la sección VALUE
    tlevels = stats_por_nivel([dict(r) for r in tracked_rows] + [dict(r) for r in system_value_rows])
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
    # Fechas para el encabezado por día de Receipts (2026-10-16, diseño
    # "Yesterday" estilo WGT aprobado por Alex): la plantilla etiqueta
    # el día más reciente como Yesterday/Today según corresponda.
    _hoy_iso = datetime.now(TZ).strftime("%Y-%m-%d")
    _ayer_iso = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    # Agrupado por día para Receipts conservando el orden del archivo
    # (día más reciente primero): el groupby de Jinja ordena las claves
    # y pondría el día más viejo arriba.
    recientes_dias = []
    for _r in recientes:
        if recientes_dias and recientes_dias[-1]["fecha"] == _r.get("fecha"):
            recientes_dias[-1]["plays"].append(_r)
        else:
            recientes_dias.append({"fecha": _r.get("fecha", ""),
                                   "fecha_iso": _r.get("fecha_iso", ""),
                                   "plays": [_r]})
    # Vista previa admin (pedido Alex 2026-09-30): ?preview=locked muestra el
    # home como lo ve un miembro sin Elite (teaser bloqueado + pancarta $1).
    preview_locked = is_admin_for(user) and request.args.get("preview") == "locked"
    # Lluvia de dinero (Alex 2026-10-13): SOLO cuando ayer quedó positivo,
    # con burbuja de resultados, hasta que empieza el primer partido de hoy
    # — ver celebracion_hoy().
    _lux = home_lux_enabled()
    # LIVE / PENDING en la tarjeta de estado (2026-10-15, pedido de Alex:
    # "a la tarjeta métele un live en movimiento y un pendiente cuando no
    # estén"): LIVE si alguna jugada de hoy ya empezó y sigue dentro de
    # su ventana de juego (~3 h 45 m); PENDING si la card está publicada
    # y ningún juego ha empezado. Nunca rompe el home: ante cualquier
    # problema de parseo, ambos quedan en False.
    hay_live = False
    esperando = False
    try:
        _now = datetime.now(TZ)
        _starts = []
        for _p in plays:
            _gt = (_p.get("game_time_et") or _p.get("hora") or "")
            _gt = _gt.replace(" ET", "").strip()
            if not _gt:
                continue
            try:
                _t = datetime.strptime(_gt, "%I:%M %p")
            except ValueError:
                continue
            _starts.append(datetime(_now.year, _now.month, _now.day,
                                    _t.hour, _t.minute, tzinfo=TZ))
        if _starts:
            esperando = all(s > _now for s in _starts)
            hay_live = any(s <= _now <= s + timedelta(hours=3, minutes=45)
                           for s in _starts)
    except Exception:
        hay_live = False
        esperando = False
    # Carta bloqueada con el TICKET real difuminado (2026-10-20, pedido de
    # Alex: «haz la jugada con su ticket, que se vea bloqueada»): quien no
    # tiene Platinum ve la foto del ticket Platinum de hoy con el blur
    # HORNEADO en la copia de nombre neutro (locked_ticket_url_for) — la
    # foto original y su nombre nunca llegan a su HTML. Sin ticket o ante
    # cualquier fallo: None y la carta sale con su panel bloqueado.
    locked_ticket_url = None
    if not card_pendiente and not (platinum_unlocked_for(user)
                                   and not preview_locked):
        _plat = next((p for p in plays
                      if p.get("nivel") == "ELITE" and p.get("comprobante")),
                     None)
        if _plat:
            locked_ticket_url = locked_ticket_url_for(
                _plat, datetime.now(TZ).strftime("%Y-%m-%d"))
    # Vista previa admin CON ticket real (2026-10-20, pedido de Alex:
    # «hazlo con la Platinum de Tampa, usa ese recibo»): sin card de hoy
    # publicada, la carta bloqueada de la preview se renderiza con la
    # Platinum más reciente con comprobante del archivo y su imagen
    # bloqueada (blur horneado con la fecha de ESA jugada). SOLO entra
    # ?preview=locked (admin): las plantillas leen preview_locked_play
    # únicamente en la rama sin jugada de hoy, y el flujo de miembros
    # nunca lo recibe con valor — su estado pendiente no cambia.
    preview_locked_play = None
    if preview_locked and card_pendiente:
        preview_locked_play, _prev_fecha = platinum_reciente_con_ticket()
        if preview_locked_play:
            locked_ticket_url = locked_ticket_url_for(
                preview_locked_play, _prev_fecha)
    # Estados sellados de la sección Platinum (Alex, 2026-10-20): para
    # quien NO tiene Platinum, si la card de hoy NO está publicada de
    # verdad, la sección NO existe (locked_plays vacío: la plantilla no
    # renderiza ni el encabezado). Solo la publicación real — el mismo
    # estado card_publicada_hoy() de siempre, nunca un anuncio, el
    # horario, una preview ni una candidata del Admin — enciende la
    # carta original bloqueada con las ELITE de hoy sanitizadas
    # (_locked_play_view). La preview admin (?preview=locked) es solo la
    # herramienta de verificación de Alex: sin card de hoy usa el
    # fallback de la Platinum reciente con ticket (Tampa); el flujo de
    # miembros jamás recibe ese fallback.
    locked_plays = []
    if not platinum_unlocked_for(user) or preview_locked:
        if _card_publicada:
            _hoy_lock = datetime.now(TZ).strftime("%Y-%m-%d")
            for _p in plays:
                if _p.get("nivel") == "ELITE":
                    _tk = (locked_ticket_url_for(_p, _hoy_lock)
                           if _p.get("comprobante") else None)
                    locked_plays.append(_locked_play_view(_p, _tk))
        elif preview_locked and preview_locked_play:
            locked_plays.append(
                _locked_play_view(preview_locked_play, locked_ticket_url))
    return render_template(
        # Tema v2 (moderno) es el predeterminado desde 2026-10-02 (pedido Alex);
        # v1 (clásico) queda como reversión vía /theme/v1. Diseño lux
        # (2026-10-12): home premium salvo revert del admin (site_settings).
        "home.html" if session.get("theme") == "v1" else ("home_lux.html" if _lux else "home_v2.html"),
        plays=plays,
        card_pendiente=card_pendiente,
        hay_live=hay_live,
        esperando=esperando,
        tracked_ids=tracked_ids,
        program=program,
        fecha=fecha_larga(),
        fecha_corta=fecha_corta(),
        saludo=saludo_hoy(),
        racha=checkin_streak(db, session["user_id"]),
        bankroll=user["bankroll"] if user else None,
        stake_mode=user["stake_mode"] if user else "units",
        platinum_unlocked=(platinum_unlocked_for(user) and not preview_locked),
        res=program_stats(),
        pcurve=program_curve(),
        diseno_lux=_lux,
        celebracion=celebracion_hoy(),
        dias=load_archive(),
        snapshot=performance_snapshot(),
        now_day_of_year=datetime.now().timetuple().tm_yday,
        leccion=load_masterclass(),
        archivo_mc=load_masterclass_archivo(),
        tstats=tstats,
        tlevels=tlevels,
        tcurve=tcurve,
        pstreak=program_streak(),
        streak=program_streak(),
        resumen_ayer=resumen_ayer(),
        recientes=recientes,
        recientes_dias=recientes_dias,
        hoy_iso=_hoy_iso,
        ayer_iso=_ayer_iso,
        recientes_value=recientes_value(),
        hot_cards=hot_cards,
        record_stats=official_tracker_full(),
        last_elite_win=last_elite_win(),
        locked_ticket_url=locked_ticket_url,
        locked_plays=locked_plays,
        preview_locked_play=preview_locked_play,
        es_admin=is_admin_for(user),
        es_alex=is_alex_member(user),
        skip_splash=request.args.get("splash") == "0",
        # miembro_platinum con preview: en ?preview=locked el banner de
        # invitación VIP también se muestra (pedido Alex 2026-10-02).
        miembro_platinum=(platinum_unlocked_for(user) and not preview_locked),
    )


def _send_meta_capi_registration(email, event_id, ip, user_agent, fbp, fbc, source_url):
    """Conversions API de Meta (2026-10-14): envía el CompleteRegistration
    también desde el servidor, con el MISMO event_id que dispara el pixel
    del navegador en /bienvenida, para que Meta deduplique el evento y
    active las coincidencias avanzadas (email hasheado SHA-256).

    No-op silencioso si no existe la env var META_CAPI_TOKEN. El envío va
    SIEMPRE en un hilo de fondo con timeout corto y try/except total: un
    fallo de Meta NUNCA puede bloquear ni romper un registro. El token
    jamás se loguea ni se imprime."""
    token = os.environ.get("META_CAPI_TOKEN")
    if not token or not email:
        return

    def _go():
        try:
            user_data = {
                "em": [hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()],
            }
            if ip:
                user_data["client_ip_address"] = ip
            if user_agent:
                user_data["client_user_agent"] = user_agent
            if fbp:
                user_data["fbp"] = fbp
            if fbc:
                user_data["fbc"] = fbc
            payload = {
                "data": [{
                    "event_name": "CompleteRegistration",
                    "event_time": int(time.time()),
                    "event_id": event_id,
                    "event_source_url": source_url,
                    "action_source": "website",
                    "user_data": user_data,
                }],
                "access_token": token,
            }
            req = urllib.request.Request(
                "https://graph.facebook.com/v21.0/1625430959036525/events",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                        "Mobile/15E148 Safari/604.1"
                    ),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                resp.read()
        except Exception:
            pass

    try:
        threading.Thread(target=_go, daemon=True).start()
    except Exception:
        pass


_GEO_CACHE = {}


def _pais_visitante():
    """País del visitante por geo-IP (ip-api.com, sin clave). Caché por
    IP de 24 h; timeout corto y cualquier fallo devuelve '' (la página
    sale en inglés — esto nunca puede romper el registro)."""
    try:
        ip = (request.headers.get("X-Forwarded-For", "")
              or "").split(",")[0].strip() or (request.remote_addr or "")
    except Exception:
        return ""
    if not ip or ip.startswith(("127.", "10.", "172.", "192.168.", "::1")):
        return ""
    _hit = _GEO_CACHE.get(ip)
    if _hit and time.time() - _hit[0] < 86400:
        return _hit[1]
    pais = ""
    try:
        _req = urllib.request.Request(
            f"http://ip-api.com/json/{ip}?fields=status,countryCode",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(_req, timeout=1.5) as _r:
            _d = json.loads(_r.read() or b"{}")
        if _d.get("status") == "success":
            pais = (_d.get("countryCode") or "").upper()
    except Exception:
        pais = ""
    _GEO_CACHE[ip] = (time.time(), pais)
    return pais


def _registro_semana():
    """Récord REAL de los últimos 7 días (franja caliente de /registro,
    2026-10-21): se calcula del archivo oficial en cada carga — si la
    semana cambia, la franja cambia sola; sin jugadas, no se muestra."""
    try:
        hoy = datetime.now(TZ).date()
        desde = (hoy - timedelta(days=7)).isoformat()
        hasta = hoy.isoformat()
        g = p = 0
        profit = 0.0
        for d in load_archive():
            f = str(d.get("fecha") or "")[:10]
            if not f or f < desde or f > hasta:
                continue
            for j in d.get("jugadas", []) or []:
                if j.get("resultado") not in ("WON", "LOST") or j.get("bloqueada"):
                    continue
                if j["resultado"] == "WON":
                    g += 1
                else:
                    p += 1
                profit += float(j.get("profit") or 0.0)
        total = g + p
        if not total:
            return None
        return {
            "ganadas": g, "perdidas": p, "total": total,
            "win_rate": f"{g / total * 100:.1f}",
            "profit_display": (f"+${profit:,.0f}" if profit >= 0 else f"-${abs(profit):,.0f}"),
        }
    except Exception:
        return None


@app.route("/registro", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))
    # Prueba social EN VIVO para la landing de registro (todo desde el
    # archivo oficial; rediseño de conversión aprobado por Alex 2026-10-13:
    # unidades al frente, récord completo, resultados recientes, fondo de
    # futbolista). Nada hardcodeado.
    try:
        _stats = program_stats()
        _gold = _stats["niveles"]["GOLD"]
    except Exception:
        _stats = {"ganadas": 0, "perdidas": 0, "win_rate": 0.0,
                  "profit_units_display": "+0.00u"}
        _gold = {"ganadas": 0, "perdidas": 0, "win_rate": 0.0,
                 "net_display": "$0", "profit": 0.0}
    _gold = dict(_gold)
    _gold["units_display"] = f"{float(_gold.get('profit') or 0) / 25.0:+.2f}u"
    try:
        _ayer = resumen_ayer()
    except Exception:
        _ayer = None
    if _ayer:
        _ayer = dict(_ayer)
        _ayer["units_display"] = f"{float(_ayer.get('profit') or 0) / 25.0:+.2f}u"
    try:
        _recientes = recientes_oficiales(4)
    except Exception:
        _recientes = []
    for _r in _recientes:
        _r["units_display"] = f"{float(_r.get('profit') or 0) / 25.0:+.2f}u"
    _drop = "1:00 PM ET"  # 2026-10-20, orden de Alex: la card sale a la 1:00 PM ET TODOS los dias (antes 2:00 PM, orden 2026-10-17)
    _semana = _registro_semana()
    _reg_ctx = {"gold": _gold, "stats": _stats, "ayer": _ayer, "semana": _semana,
                "recientes": _recientes, "drop_time": _drop,
                "turnstile_key": TURNSTILE_SITE_KEY}
    # Idioma de la landing (2026-10-14, pedido de Alex): manda ?lang=;
    # si no, la cookie; si no, SOLO República Dominicana (geo-IP) recibe
    # español — cualquier otro país ve inglés, siempre.
    _lang = (request.args.get("lang") or "").strip().lower()
    if _lang not in ("es", "en"):
        _lang = (request.cookies.get("tst_lang") or "").strip().lower()
    if _lang not in ("es", "en"):
        _lang = "es" if _pais_visitante() == "DO" else "en"
    _tpl = "register_es.html" if _lang == "es" else "register.html"
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        sms_optin = 1 if request.form.get("sms_optin") == "1" and phone else 0
        if not nombre or not email or len(password) < 6:
            flash("Escribe tu nombre, un correo válido y una contraseña de al menos 6 caracteres." if _lang == "es" else "Enter your name, a valid email and a password of at least 6 characters.", "error")
            return render_template(_tpl, **_reg_ctx), 400
        # Captcha anti-robots (2026-10-18): sin pasar Turnstile no se crea
        # ninguna cuenta. Sin llaves configuradas, _turnstile_ok() no bloquea.
        if not _turnstile_ok():
            flash("Completa la verificación «No soy un robot» e inténtalo de nuevo." if _lang == "es" else "Please complete the 'I'm not a robot' check and try again.", "error")
            return render_template(_tpl, **_reg_ctx), 400
        db = get_db()
        exists = db.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            _ya_verificado = 1
            try:
                _ya_verificado = exists["email_verified"]
            except Exception:
                _ya_verificado = 1
            if _ya_verificado:
                flash("Ese correo ya está registrado. Inicia sesión." if _lang == "es" else "That email is already registered. Log in.", "error")
                return render_template(_tpl, **_reg_ctx), 400
            # Se registró antes pero nunca activó: token nuevo y reenvío.
            _tok = secrets.token_urlsafe(32)
            db.execute("UPDATE users SET activation_token = ? WHERE id = ?",
                       (_tok, exists["id"]))
            db.commit()
            send_activation_email(nombre, email, _tok, _lang)
            return render_template("confirmacion.html", email=email, nombre=nombre, lang=_lang, reenviado=True)
        # La cuenta nace SIN ACTIVAR (2026-10-18, orden de Alex): no hay
        # sesión, no hay mes gratis ni bienvenida hasta que el dueño toque
        # el enlace del correo de activación (/activar/<token>).
        _tok = secrets.token_urlsafe(32)
        insert_returning_id(
            db,
            "INSERT INTO users (nombre, email, password_hash, phone, sms_optin, created_at, lang, email_verified, activation_token) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (nombre, email, hash_password(password), phone or None, sms_optin, now_iso(), _lang, _tok),
        )
        db.commit()
        send_activation_email(nombre, email, _tok, _lang)
        return render_template("confirmacion.html", email=email, nombre=nombre, lang=_lang, reenviado=False)
    _resp = make_response(render_template(_tpl, **_reg_ctx))
    _resp.set_cookie("tst_lang", _lang, max_age=30 * 86400, samesite="Lax")
    return _resp


@app.route("/reenviar-activacion", methods=["POST"])
def reenviar_activacion():
    """Reenvío del correo de activación desde la pantalla de
    confirmación (2026-10-21): regenera el token y reenvía solo si la
    cuenta existe y sigue sin verificar. La respuesta es idéntica en
    todos los casos (no revela si el correo existe) y hay un freno de
    60 segundos por sesión."""
    email = (request.form.get("email") or "").strip().lower()
    lang = (request.form.get("lang") or "en").strip().lower()
    if lang not in ("es", "en"):
        lang = "en"
    try:
        ultimo = float(session.get("resend_ts") or 0)
    except (TypeError, ValueError):
        ultimo = 0
    if email and time.time() - ultimo >= 60:
        try:
            db = get_db()
            u = db.execute("SELECT id, nombre, email_verified, lang FROM users WHERE email = ?", (email,)).fetchone()
            if u and not u["email_verified"]:
                _tok = secrets.token_urlsafe(32)
                db.execute("UPDATE users SET activation_token = ? WHERE id = ?", (_tok, u["id"]))
                db.commit()
                send_activation_email(u["nombre"], email, _tok, u["lang"] or lang)
            session["resend_ts"] = time.time()
        except Exception:
            pass
    return render_template("confirmacion.html", email=email, lang=lang, reenviado=True)


@app.route("/activar/<token>")
def activar_cuenta(token):
    """Activa una cuenta nueva desde el enlace del correo (2026-10-18).

    Al activar: se marca verificada, entra la sesión, sale la bienvenida
    y el aviso de miembro nuevo, y se reporta el registro a Meta (pixel +
    CAPI con el mismo event_id, como antes).

    Desde 2026-10-19 (orden de Alex) activar YA NO otorga Platinum: el
    mes gratis murió. Platinum arranca solo con el checkout de $1
    (webhook checkout.session.completed). Las cuentas con reloj de mes
    gratis ya vigente no se tocan: su platinum_trial_ends sigue corriendo
    hasta su fecha.
    """
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE activation_token = ?",
                      (token,)).fetchone()
    if not user:
        flash("That activation link is no longer valid. Log in or register again.", "error")
        return redirect(url_for("login"))
    uid = user["id"]
    nombre = user["nombre"]
    email = user["email"]
    db.execute("UPDATE users SET email_verified = 1, activation_token = NULL, activated_at = ? WHERE id = ?",
               (now_iso(), uid))
    db.commit()
    # Admin solo si la cuenta ya trae la marca en la BD (2026-10-18:
    # ADMIN_EMAIL es el correo de avisos, no una identidad).
    es_admin = is_admin_for(user)
    if es_admin:
        db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
        db.commit()
    # Sin grant de Platinum al activar (2026-10-19): la cuenta queda en
    # nivel Gold/free hasta que pague el checkout de $1. Nada que hacer
    # aquí a propósito — platinum_unlocked y platinum_trial_ends se dejan
    # exactamente como están.
    log_notificacion(db, "miembro_nuevo", f"New member: {nombre}", email)
    session["user_id"] = uid
    session["nombre"] = nombre
    session["is_admin"] = es_admin
    session.permanent = True
    _registrar_actividad(uid, "login")
    notify_new_member(nombre, email)
    # Pedido Alex 2026-10-05: el email de bienvenida llega a TODOS, y desde
    # 2026-10-18 llega al ACTIVAR la cuenta (cuenta real, correo real).
    try:
        send_welcome_email(nombre, email)
        db.execute("UPDATE users SET welcome_email_sent = 1 WHERE id = ?", (uid,))
        db.commit()
    except Exception:
        pass
    # Pixel de Meta: la bienvenida reporta el registro una sola vez.
    # CAPI (2026-10-14): UN event_id compartido entre el servidor (aquí) y
    # el pixel del navegador (en /bienvenida) para que Meta deduplique.
    fb_event_id = uuid.uuid4().hex
    session["fb_reg"] = 1
    session["fb_event_id"] = fb_event_id
    client_ip = (request.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip() or request.remote_addr
    _send_meta_capi_registration(
        email,
        fb_event_id,
        client_ip,
        request.user_agent.string if request.user_agent else "",
        request.cookies.get("_fbp"),
        request.cookies.get("_fbc"),
        request.url,
    )
    return redirect(url_for("bienvenida"))


# Links rastreados (pedido Alex 2026-10-02): cada clic se guarda en
# link_clicks y redirige al destino. Un watcher en la VM lee
# /api/clicks-feed y avisa a Alex por Telegram al instante.
TRACKED_LINKS = {
    "ig": "/registro",  # link del anuncio de Instagram/Facebook
}


# Anti-spam de avisos de clic (2026-10-04, pedido por Alex): los crawlers de
# Meta (facebookexternalhit) golpean /go/ig en ráfagas durante la revisión del
# anuncio y cada golpe mandaba un aviso. Ahora: 1) no se avisa si el User-Agent
# es de bot/crawler; 2) como máximo un aviso cada 10 min por slug.
_BOT_UA_PATTERNS = (
    "facebookexternalhit", "facebot", "meta-external",
    "googlebot", "bingbot", "slurp", "duckduckbot", "baiduspider",
    "yandexbot", "sogou", "twitterbot", "linkedinbot", "slackbot",
    "discordbot", "telegrambot", "whatsapp", "crawl", "spider",
)
_CLICK_NOTIFY_COOLDOWN_SEC = 600
_last_click_notify = {}


def _is_bot_ua(ua):
    ua_l = (ua or "").lower()
    return any(p in ua_l for p in _BOT_UA_PATTERNS)


def _notify_click_telegram(slug):
    """Aviso en tiempo real por Telegram cuando tocan un link rastreado.

    Solo funciona si Render tiene TELEGRAM_BOT_TOKEN. Nunca bloquea:
    timeout corto y todo error se traga en silencio (el watcher de la VM
    es el respaldo). No avisa bots/crawlers ni más de 1 vez cada 10 min.
    """
    if not TELEGRAM_BOT_TOKEN or not CLICK_NOTIFY_CHAT_ID:
        return False
    try:
        ua = request.headers.get("User-Agent", "") or ""
    except Exception:
        ua = ""
    if _is_bot_ua(ua):
        print("[click-notify] bot UA, sin aviso", flush=True)
        return False
    now = time.time()
    if now - _last_click_notify.get(slug, 0) < _CLICK_NOTIFY_COOLDOWN_SEC:
        print("[click-notify] cooldown, sin aviso", flush=True)
        return False
    try:
        payload = json.dumps(
            {
                "chat_id": CLICK_NOTIFY_CHAT_ID,
                "text": "🔗 Alguien tocó tu link de Instagram",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            ok = resp.status == 200
            print(f"[click-notify] telegram direct: status={resp.status}", flush=True)
            if ok:
                _last_click_notify[slug] = now
            return ok
    except Exception as e:
        print(f"[click-notify] telegram direct FAILED: {type(e).__name__}: {e}", flush=True)
        return False


_last_login_notify = {}
_LOGIN_NOTIFY_COOLDOWN_SEC = 10  # 10 seg por miembro (solo evita duplicados por doble-carga)


def _notify_login_telegram(user):
    """Aviso a Alex por Telegram cada vez que un miembro entra al Dashboard.

    Pedido por Alex 2026-10-05: notificación en CADA visita al Dashboard
    (no solo al login, porque la sesión queda abierta permanente).
    CAMBIO 2026-10-10 (orden de Alex): la cuenta de miembro de Alex
    (pena.manuel@myyahoo.com) YA NO se notifica — solo avisa cuando
    OTROS miembros entran. Se excluyen logins de admin. No avisa
    bots/crawlers. Cooldown de solo 60 seg por miembro (únicamente para
    no duplicar por doble-carga de la página).
    Nunca bloquea la carga: timeout corto y todo error se traga en silencio.
    """
    if not TELEGRAM_BOT_TOKEN or not CLICK_NOTIFY_CHAT_ID:
        return False
    email = (_uget(user, "email") or "").strip().lower()
    # Alex no se notifica a sí mismo (2026-10-10, orden de Alex): solo cuando otros miembros entran.
    if email == ALEX_MEMBER_EMAIL:
        return False
    if _uget(user, "is_admin"):
        return False
    try:
        ua = request.headers.get("User-Agent", "") or ""
    except Exception:
        ua = ""
    if _is_bot_ua(ua):
        return False
    now = time.time()
    key = f"login:{_uget(user, 'id')}"
    if now - _last_login_notify.get(key, 0) < _LOGIN_NOTIFY_COOLDOWN_SEC:
        return False
    nombre = _uget(user, "nombre") or _uget(user, "email") or "?"
    email = _uget(user, "email") or ""
    elite = "⭐ Platinum" if _uget(user, "platinum_unlocked") else "🟡 Free"
    try:
        hora = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    except Exception:
        hora = ""
    text = f"👤 {nombre} ({elite}) entró al Dashboard\n{email}\n{hora}".strip()
    try:
        payload = json.dumps(
            {"chat_id": CLICK_NOTIFY_CHAT_ID, "text": text}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            ok = resp.status == 200
            if ok:
                _last_login_notify[key] = now
            return ok
    except Exception as e:
        print(f"[login-notify] telegram FAILED: {type(e).__name__}: {e}", flush=True)
        return False


@app.route("/go/<slug>")
def tracked_link(slug):
    dest = TRACKED_LINKS.get(slug)
    if not dest:
        return render_template("404.html"), 404
    try:
        db = get_db()
        db.execute(
            "INSERT INTO link_clicks (slug, created_at, ip, ua, referer) VALUES (?, ?, ?, ?, ?)",
            (
                slug,
                now_iso(),
                (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "")[:64],
                (request.headers.get("User-Agent", "") or "")[:300],
                (request.headers.get("Referer", "") or "")[:300],
            ),
        )
        db.commit()
    except Exception:
        pass  # el redirect nunca se bloquea por un fallo del log
    # Aviso en tiempo real (si hay token en Render; si no, el watcher lo manda).
    try:
        _notify_click_telegram(slug)
    except Exception:
        pass
    return redirect(dest, code=302)


@app.route("/api/clicks-feed")
def api_clicks_feed():
    """Feed de clics nuevos para el watcher de la VM.

    Protegido igual que /api/push-edge: header X-Push-Key == PUSH_TRIGGER_KEY.
    Query: ?since=<ISO UTC>. Devuelve {"clicks": [{id, slug, created_at, ...}]}.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    since = request.args.get("since", "1970-01-01T00:00:00+00:00")
    db = get_db()
    rows = db.execute(
        "SELECT id, slug, created_at, ip, ua, referer FROM link_clicks "
        "WHERE created_at > ? ORDER BY id ASC LIMIT 200",
        (since,),
    ).fetchall()
    return jsonify(
        {
            "clicks": [
                {
                    "id": r["id"],
                    "slug": r["slug"],
                    "created_at": r["created_at"],
                    "ip": r["ip"],
                    "ua": r["ua"],
                    "referer": r["referer"],
                }
                for r in rows
            ]
        }
    )


@app.route("/bienvenida")
@login_required
def bienvenida():
    """Página de bienvenida tras el registro: explica el programa.
    Si el miembro acaba de registrarse, la base dispara una sola vez el
    evento CompleteRegistration del pixel de Meta (2026-10-14)."""
    fb_reg = bool(session.pop("fb_reg", None))
    fb_event_id = session.pop("fb_event_id", None)
    return render_template("bienvenida.html", nombre=session.get("nombre", ""), fb_reg=fb_reg, fb_event_id=fb_event_id)


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
        # is_admin sale SOLO de la base de datos (2026-10-18): ADMIN_EMAIL
        # es el correo de AVISOS del admin (la cuenta personal de Alex),
        # NO una marca de identidad: su cuenta de miembro usa ese mismo
        # correo y debe verse como miembro normal.
        es_admin = is_admin_for(user)
        # Arranque: si todavía no existe ningún admin, el primero en entrar lo es.
        if not es_admin:
            fila = db.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()
            n_admin = fila["n"] if isinstance(fila, dict) else fila[0]
            if n_admin == 0:
                db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
                db.commit()
                es_admin = True
        # Cuenta SIN ACTIVAR (2026-10-18): llenó el formulario pero nunca
        # tocó el enlace del correo — no entra; se le reenvía la activación
        # al momento (token nuevo). El admin nunca se bloquea a sí mismo.
        _verificado = 1
        try:
            _verificado = user["email_verified"]
        except Exception:
            _verificado = 1
        if not _verificado and es_admin:
            db.execute("UPDATE users SET email_verified = 1, activation_token = NULL WHERE id = ?",
                       (user["id"],))
            db.commit()
            _verificado = 1
        if not _verificado:
            _tok = secrets.token_urlsafe(32)
            db.execute("UPDATE users SET activation_token = ? WHERE id = ?",
                       (_tok, user["id"]))
            db.commit()
            _ulang = "en"
            try:
                _ulang = user["lang"] or "en"
            except Exception:
                _ulang = "en"
            send_activation_email(user["nombre"], email, _tok, _ulang)
            flash("Your account isn't activated yet — we just re-sent the activation email. Tap the link inside to get in.", "error")
            return render_template("login.html"), 403
        session["user_id"] = user["id"]
        session["nombre"] = user["nombre"]
        session["is_admin"] = es_admin
        session.permanent = True  # 2026-09-25: cookie persistente 30 días (antes iOS la borraba → logout)
        _registrar_actividad(user["id"], "login")
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


def _past_due_billing_info(user):
    """Datos de facturación para la sección past-due de /cuenta (modelo WGT).

    Lee de Stripe: tarjeta por defecto (marca, últimos 4, vencimiento),
    precio/intervalo de la suscripción, próxima fecha de cobro y URL de la
    factura abierta (para el botón "Update card & restore access").
    Nunca rompe la página: ante cualquier fallo devuelve los campos en None.
    """
    info = {"card_brand": None, "card_last4": None, "card_exp": None,
            "plan_price": None, "plan_interval": None,
            "next_billing": None, "pay_url": None}
    if not stripe_configurado():
        return info
    customer_id = ""
    try:
        customer_id = (user["stripe_customer_id"] or "").strip()
    except Exception:
        pass
    if not customer_id:
        return info
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        try:
            cu = stripe.Customer.retrieve(customer_id)
            pm_id = ((cu.get("invoice_settings") or {}).get("default_payment_method")
                     or cu.get("default_source"))
            if pm_id:
                pm = stripe.PaymentMethod.retrieve(pm_id)
                card = pm.get("card") or {}
                info["card_brand"] = (card.get("brand") or "").replace("_", " ").title()
                info["card_last4"] = card.get("last4")
                if card.get("exp_month") and card.get("exp_year"):
                    info["card_exp"] = "%02d/%d" % (card["exp_month"], card["exp_year"])
        except Exception:
            pass
        try:
            subs = stripe.Subscription.list(customer=customer_id, limit=1)
            if subs and subs.data:
                sub = subs.data[0]
                items = (sub.get("items") or {}).get("data") or []
                if items:
                    price = items[0].get("price") or {}
                    if price.get("unit_amount") is not None:
                        info["plan_price"] = price["unit_amount"] / 100.0
                    info["plan_interval"] = ((price.get("recurring") or {}).get("interval") or "")
                cpe = sub.get("current_period_end")
                if cpe:
                    info["next_billing"] = datetime.fromtimestamp(
                        cpe, tz=timezone.utc).astimezone(TZ).strftime("%d %b %Y")
        except Exception:
            pass
        try:
            invs = stripe.Invoice.list(customer=customer_id, status="open", limit=1)
            if invs and invs.data:
                info["pay_url"] = invs.data[0].get("hosted_invoice_url")
        except Exception:
            pass
    except Exception:
        pass
    return info


def _stripe_subscription_info(user):
    """Info de la suscripción activa para la pestaña Subscriptions de /cuenta
    (2026-10-12, página Account estilo WGT pedida por Alex). Nunca rompe:
    devuelve None si no hay cliente/suscripción o Stripe falla."""
    if not stripe_configurado() or not user:
        return None
    try:
        customer_id = (user["stripe_customer_id"] or "").strip()
    except Exception:
        customer_id = ""
    if not customer_id:
        return None
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        subs = stripe.Subscription.list(customer=customer_id, limit=1)
        if not subs or not subs.data:
            return None
        sub = subs.data[0]
        info = {"status": sub.get("status") or "",
                "price": None, "interval": "",
                "next_billing": None, "cancel_at_period_end": bool(sub.get("cancel_at_period_end"))}
        items = (sub.get("items") or {}).get("data") or []
        if items:
            price = items[0].get("price") or {}
            if price.get("unit_amount") is not None:
                info["price"] = price["unit_amount"] / 100.0
            info["interval"] = ((price.get("recurring") or {}).get("interval") or "")
        cpe = sub.get("current_period_end")
        if cpe:
            info["next_billing"] = datetime.fromtimestamp(
                cpe, tz=timezone.utc).astimezone(TZ).strftime("%d %b %Y")
        return info
    except Exception:
        return None


def _stripe_payment_history(user, limit=20):
    """Historial de pagos (facturas de Stripe) para la pestaña Payments de
    /cuenta (2026-10-12). Devuelve lista de dicts; [] ante cualquier fallo."""
    if not stripe_configurado() or not user:
        return []
    try:
        customer_id = (user["stripe_customer_id"] or "").strip()
    except Exception:
        customer_id = ""
    if not customer_id:
        return []
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        invs = stripe.Invoice.list(customer=customer_id, limit=limit)
        out = []
        for inv in (invs.data if invs else []):
            st = inv.get("status") or ""
            label = {"paid": "Complete", "open": "Pending"}.get(st, "Failed")
            desc = ""
            try:
                lines = (inv.get("lines") or {}).get("data") or []
                if lines:
                    desc = lines[0].get("description") or ""
            except Exception:
                pass
            if not desc:
                desc = inv.get("description") or "The Sharp Team — Platinum Weekly"
            created = inv.get("created")
            fecha = (datetime.fromtimestamp(created, tz=timezone.utc)
                     .astimezone(TZ).strftime("%Y-%m-%d") if created else "")
            out.append({"desc": desc, "fecha": fecha,
                        "amount": (inv.get("amount_paid") or inv.get("amount_due") or 0) / 100.0,
                        "status": label})
        return out
    except Exception:
        return []


def _stripe_force_english(customer_id):
    """Fuerza inglés en las páginas de Stripe que ve el miembro (pedido por Alex
    2026-10-08: el portal de Stripe salía en español según el idioma del teléfono).
    `preferred_locales=['en']` en el Customer hace que facturas hospedadas y
    portal se muestren en inglés. Nunca rompe el flujo: silencioso ante fallos."""
    if not customer_id or not stripe_configurado():
        return
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        stripe.Customer.modify(str(customer_id), preferred_locales=["en"])
    except Exception:
        pass


@app.route("/reportar", methods=["GET", "POST"])
@login_required
def reportar():
    """Reporta un error o sugiere una mejora (2026-10-17, pedido de Alex):
    llega al centro de notificaciones del admin con el email del miembro."""
    user = current_user()
    tipo = (request.args.get("tipo") or request.form.get("tipo") or "error").strip().lower()
    if tipo not in ("error", "idea"):
        tipo = "error"
    enviado = False
    if request.method == "POST":
        mensaje = (request.form.get("mensaje") or "").strip()[:2000]
        pagina = (request.form.get("pagina") or "").strip()[:200]
        if mensaje:
            db = get_db()
            nombre = (user["nombre"] or "") if user else ""
            email = (user["email"] or "") if user else ""
            titulo = ("⚠️ Error reportado" if tipo == "error" else "💡 Sugerencia") + (f" — {nombre}" if nombre else "")
            detalle = mensaje + f"\n— {email} · página: {pagina or '—'}"
            log_notificacion(db, "reporte_error" if tipo == "error" else "sugerencia", titulo, detalle)
            db.commit()
            enviado = True
    return render_template("reportar.html", tipo=tipo, enviado=enviado)


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
    # Estado past-due (modelo WGT): datos de facturación desde Stripe.
    past_due = bool(user and user["stripe_past_due"])
    billing = _past_due_billing_info(user) if past_due else None
    # Página Account con pestañas (2026-10-12, estilo WGT pedido por Alex):
    # suscripción e historial desde Stripe + perfil de apuestas guardado.
    sub_info = _stripe_subscription_info(user)
    payments = _stripe_payment_history(user)
    try:
        bet_books_saved = json.loads(user["bet_books"] or "[]") if user else []
    except Exception:
        bet_books_saved = []
    def _ctx():
        return dict(
            bankroll=user["bankroll"] if user else None,
            cancel_at=user["cancel_requested_at"] if user else None,
            platinum=platinum_unlocked_for(user),
            stake_mode=user["stake_mode"] if user else "units",
            tg_link=tg_link, tg_linked=tg_linked,
            past_due=past_due, billing=billing,
            member_since=member_since,
            u=user, sub_info=sub_info, payments=payments,
            bet_books_saved=bet_books_saved,
            bet_books_all=BET_BOOKS, bet_states_all=BET_STATES,
        )
    try:
        member_since = datetime.fromisoformat(
            user["created_at"]).strftime("%d %b %Y") if user and user["created_at"] else None
    except Exception:
        member_since = None
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
            return render_template("cuenta.html", **_ctx()), 400


        db.execute("UPDATE users SET bankroll = ? WHERE id = ?", (val, session["user_id"]))
        db.commit()
        flash(f"Bankroll saved: ${val:,.2f}.", "ok")
        return redirect(url_for("cuenta"))
    return render_template("cuenta.html", **_ctx())


# Libros del perfil de apuestas (pestaña Preferences, 2026-10-12).
BET_BOOKS = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "ESPN BET",
             "Fanatics", "BetRivers", "Hard Rock Bet", "bet365", "Fliff",
             "Novig", "ProphetX"]
BET_STATES = ["Arizona", "Arkansas", "Colorado", "Connecticut", "Delaware",
              "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
              "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
              "Mississippi", "Missouri", "Nevada", "New Hampshire",
              "New Jersey", "New York", "North Carolina", "Ohio", "Oregon",
              "Pennsylvania", "Rhode Island", "Tennessee", "Vermont",
              "Virginia", "Washington DC", "West Virginia", "Wyoming", "Other"]


@app.route("/cuenta/perfil", methods=["POST"])
@login_required
def cuenta_perfil():
    """Guarda nombre y dirección postal (pestaña Profile, 2026-10-12)."""
    db = get_db()
    nombre = (request.form.get("nombre") or "").strip()[:120]
    if not nombre:
        flash("Your name can't be empty.", "error")
        return redirect(url_for("cuenta"))
    db.execute(
        "UPDATE users SET nombre = ?, addr_line1 = ?, addr_line2 = ?,"
        " addr_city = ?, addr_state = ?, addr_zip = ?, addr_country = ?"
        " WHERE id = ?",
        (nombre,
         (request.form.get("addr_line1") or "").strip()[:200],
         (request.form.get("addr_line2") or "").strip()[:100],
         (request.form.get("addr_city") or "").strip()[:100],
         (request.form.get("addr_state") or "").strip()[:100],
         (request.form.get("addr_zip") or "").strip()[:20],
         (request.form.get("addr_country") or "").strip()[:60],
         session["user_id"]))
    db.commit()
    session["nombre"] = nombre
    flash("Profile saved.", "ok")
    return redirect(url_for("cuenta") + "?tab=profile")


@app.route("/cuenta/preferencias", methods=["POST"])
@login_required
def cuenta_preferencias():
    """Guarda perfil de apuestas, alertas por texto y notificaciones
    (pestaña Preferences, 2026-10-12)."""
    db = get_db()
    bet_state = (request.form.get("bet_state") or "").strip()
    if bet_state not in BET_STATES:
        bet_state = ""
    books = [b for b in request.form.getlist("bet_books") if b in BET_BOOKS]
    phone = (request.form.get("phone") or "").strip()[:30]
    sms = 1 if request.form.get("sms_optin") else 0
    db.execute(
        "UPDATE users SET bet_state = ?, bet_books = ?, phone = ?,"
        " sms_optin = ?, notif_plays = ?, notif_graded = ?,"
        " notif_reminder = ? WHERE id = ?",
        (bet_state, json.dumps(books), phone, sms,
         1 if request.form.get("notif_plays") else 0,
         1 if request.form.get("notif_graded") else 0,
         1 if request.form.get("notif_reminder") else 0,
         session["user_id"]))
    db.commit()
    flash("Preferences saved.", "ok")
    return redirect(url_for("cuenta") + "?tab=preferences")


@app.route("/cuenta/password", methods=["POST"])
@login_required
def cuenta_password():
    """Cambio de contraseña (pestaña Security, 2026-10-12)."""
    db = get_db()
    user = current_user()
    actual = request.form.get("current_password") or ""
    nueva = request.form.get("new_password") or ""
    confirma = request.form.get("confirm_password") or ""
    if not user or not check_password(actual, user["password_hash"]):
        flash("Your current password doesn't match.", "error")
        return redirect(url_for("cuenta") + "?tab=security")
    if len(nueva) < 8:
        flash("Your new password needs at least 8 characters.", "error")
        return redirect(url_for("cuenta") + "?tab=security")
    if nueva != confirma:
        flash("The new passwords don't match.", "error")
        return redirect(url_for("cuenta") + "?tab=security")
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (hash_password(nueva), session["user_id"]))
    db.commit()
    flash("Password updated.", "ok")
    return redirect(url_for("cuenta") + "?tab=security")

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password_public(token):
    """Página pública de restablecer contraseña con enlace de un solo
    uso (2026-10-16). La plataforma no tenía flujo de 'olvidé mi
    contraseña': el enlace lo genera el admin por API
    (members-reset-link) y viaja solo en el email del miembro. El
    token vence a las 24 horas y se borra al usarse."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE reset_token = ?",
                     (token,)).fetchone()
    valido = False
    if row:
        try:
            vence = datetime.fromisoformat(row["reset_expires"] or "")
            valido = datetime.now() < vence
        except (ValueError, TypeError, KeyError, IndexError):
            valido = False
    if not valido:
        return render_template("reset_password.html", estado="invalido"), 400
    if request.method == "POST":
        nueva = request.form.get("new_password") or ""
        confirma = request.form.get("confirm_password") or ""
        if len(nueva) < 8:
            return render_template("reset_password.html", estado="corta"), 400
        if nueva != confirma:
            return render_template("reset_password.html", estado="distinta"), 400
        db.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL,"
            " reset_expires = NULL WHERE id = ?",
            (hash_password(nueva), row["id"]))
        db.commit()
        return render_template("reset_password.html", estado="ok")
    return render_template("reset_password.html", estado="form")


@app.route("/cuenta/telegram-link", methods=["POST"])
@login_required
def cuenta_telegram_link():
    """Genera un código de un solo uso para vincular el Telegram del miembro.

    El miembro abre el link, pulsa START en el bot y la app vincula su
    telegram_user_id. Sirve para sacarlo del canal Sharp Club si cancela.
    """


# --------------------------------- alertas (pedido Alex 2026-10-08, estilo WGT) --
@app.route("/alertas")
@login_required
def alertas():
    """Página de preferencias de notificaciones del miembro."""
    db = get_db()
    u = db.execute(
        "SELECT notif_plays, notif_graded, notif_live, notif_reminder, notif_ev, notif_price, notif_sharp, notif_masterclass"
        " FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    prefs = dict(u) if u else {}
    return render_template("alertas.html", prefs=prefs)


@app.route("/api/alertas", methods=["POST"])
@login_required
def api_alertas():
    """Guarda una preferencia de notificación del miembro."""
    data = request.get_json(silent=True) or {}
    pref = data.get("pref", "")
    value = 1 if data.get("value") else 0
    allowed = {"notif_plays", "notif_graded", "notif_live", "notif_reminder", "notif_ev", "notif_price", "notif_sharp", "notif_masterclass"}
    if pref not in allowed:
        return jsonify({"ok": False, "error": "invalid pref"}), 400
    db = get_db()
    db.execute(f"UPDATE users SET {pref} = ? WHERE id = ?", (value, session["user_id"]))
    db.commit()
    return jsonify({"ok": True})
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


# Mapeo nombre de casa -> logo oficial (static/img/books/)
BOOK_LOGOS = {
    "novig": "books/novig.png",
    "noving": "books/novig.png",
    "draftkings": "books/draftkings.png",
    "fanduel": "books/fanduel.png",
    "betmgm": "books/betmgm.png",
    "caesars": "books/caesars.png",
    "caesarssportsbook": "books/caesars.png",
    "fanatics": "books/fanatics.png",
    "betrivers": "books/betrivers.png",
    "espnbet": "books/espnbet.png",
    "espn": "books/espnbet.png",
    "hardrock": "books/hardrock.png",
    "hardrockbet": "books/hardrock.png",
    "kalshi": "books/kalshi.png",
    "kaslshy": "books/kalshi.png",
    "thescorebet": "books/thescorebet.png",
    "thescore": "books/thescorebet.png",
}


def book_logo(book_name):
    key = re.sub(r"[^a-z0-9]", "", (book_name or "").lower())
    return BOOK_LOGOS.get(key)


# Trackear una Value Play en el tracker personal de Alex (2026-10-04, regla de Alex):
# Las VALUE solo se trackean si Alex envía el ticket (manual, vía chat).
# No hay botón "+ Track" en las cards VALUE (se quitó del template) — solo
# el flujo manual cuando él manda la foto del ticket.
@app.route("/track-value", methods=["GET", "POST"])
def track_value():
    db = get_db()
    user = current_user()
    if not (is_alex_member(user) or is_admin_for(user)):
        return redirect(url_for("home"))
    # Siempre en la cuenta personal de Alex
    if is_admin_for(user) and not is_alex_member(user):
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
        ).fetchone()
        uid = row["id"] if row else session["user_id"]
    else:
        uid = session["user_id"]
    team = (request.values.get("team") or "").strip()
    game = (request.values.get("game") or "").strip()
    price = request.values.get("price", "0")
    ev = request.values.get("ev", "0")
    if not team:
        return redirect(url_for("home"))
    try:
        cuota = int(float(price))
    except ValueError:
        cuota = 0
    try:
        edge = float(ev)
    except ValueError:
        edge = 0
    # Migración perezosa: columna comprobante en tracked_plays
    try:
        if USE_PG:
            db.execute("ALTER TABLE tracked_plays ADD COLUMN IF NOT EXISTS comprobante TEXT")
        else:
            db.execute("ALTER TABLE tracked_plays ADD COLUMN comprobante TEXT")
        db.commit()
    except Exception:
        db.rollback()
        pass
    # Stake real (del ticket); si no viene, 1u = $50
    try:
        monto = float(request.values.get("stake", "50"))
    except ValueError:
        monto = 50.0
    unidades = round(monto / 50.0, 2)
    pid = "value-%s-%s" % (
        datetime.now(TZ).strftime("%Y-%m-%d"),
        re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-"),
    )
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    comprobante = (request.values.get("comprobante") or "").strip()
    # Stake 1u del capital personal ($5,000 -> $50), o el monto real del ticket
    if db.use_pg:
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades, stake_monto, edge, resultado, comprobante, created_at)
               VALUES (?, ?, ?, 'VALUE', ?, ?, ?, ?, ?, NULL, ?, ?) ON CONFLICT DO NOTHING""",
            (uid, pid, datetime.now(TZ).strftime("%Y-%m-%d"), team, cuota, unidades, monto, _edge_num(edge), comprobante or None, now),
        )
    else:
        db.execute(
            """INSERT OR IGNORE INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades, stake_monto, edge, resultado, comprobante, created_at)
               VALUES (?, ?, ?, 'VALUE', ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (uid, pid, datetime.now(TZ).strftime("%Y-%m-%d"), team, cuota, unidades, monto, _edge_num(edge), comprobante or None, now),
        )
    db.commit()
    return redirect(url_for("tracker"))


@app.route("/balances/delete/<book_name>", methods=["POST"])
def balances_delete(book_name):
    db = get_db()
    user = current_user()
    if not (is_alex_member(user) or is_admin_for(user)):
        return redirect(url_for("home"))
    if is_admin_for(user) and not is_alex_member(user):
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
        ).fetchone()
        uid = row["id"] if row else session["user_id"]
    else:
        uid = session["user_id"]
    db.execute(
        "DELETE FROM book_balances WHERE user_id = ? AND book_name = ?",
        (uid, book_name),
    )
    db.commit()
    return redirect(url_for("balances"))


@app.route("/balances", methods=["GET", "POST"])
@login_required
def balances():
    """Balances por casa de apuesta (v1: solo cuenta personal de Alex, 2026-10-03).
    Manual por ahora; a futuro se ajustan solos al liquidarse las jugadas."""
    db = get_db()
    user = current_user()
    if not (is_alex_member(user) or is_admin_for(user)):
        return redirect(url_for("home"))
    # Los balances siempre viven en la cuenta personal de Alex (aunque entre como admin)
    if is_admin_for(user) and not is_alex_member(user):
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
        ).fetchone()
        uid = row["id"] if row else session["user_id"]
    else:
        uid = session["user_id"]
    # Crear tabla si no existe (fuera del DDL inicial para no romper el deploy)
    _ensure_book_balances_table(db)
    if request.method == "POST":
        book = (request.form.get("book_name") or "").strip()[:40]
        try:
            monto = float(request.form.get("balance") or 0)
        except ValueError:
            monto = 0
        if book:
            now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                """INSERT INTO book_balances (user_id, book_name, balance, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, book_name)
                   DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at""",
                (uid, book, monto, now),
            )
            db.commit()
        return redirect(url_for("balances"))
    rows = db.execute(
        "SELECT book_name, balance, updated_at FROM book_balances WHERE user_id = ? ORDER BY book_name",
        (uid,),
    ).fetchall()
    balances = [
        {
            "book_name": r["book_name"],
            "balance": r["balance"],
            "updated_at": r["updated_at"],
            "logo": book_logo(r["book_name"]),
        }
        for r in rows
    ]
    total = sum(float(r["balance"] or 0) for r in rows)
    return render_template("balances.html", balances=balances, total=total)


@app.route("/tracker")
def tracker():
    # Visitante SIN sesión (2026-10-19, pedido de Alex): en vez del
    # rebote al login, ve la página PÚBLICA de venta del Tracker con la
    # tarjeta WHAT MEMBERS GET — la misma que ve el miembro con sesión
    # pero sin Platinum activo. La puerta de Platinum es
    # platinum_unlocked_for() (mes gratis vigente cuenta como activo).
    if current_user() is None:
        return render_template(
            "tracker_public.html",
            public_mode=True,
            cta_url=url_for("register"),
            watch_url=url_for("register"),
            upsell_cta_url=url_for("register"),
        )
    db = get_db()
    record_checkin(db, session["user_id"])
    # Vista previa del candado (2026-10-15, para que Alex lo pruebe):
    # ?preview=locked muestra la pagina de membresia tal cual la ve
    # quien no paga, aunque la cuenta tenga acceso (patron del home).
    _u0 = current_user()
    _pv = request.args.get("preview")
    if _pv in ("locked", "vencido") and (
            is_admin_for(_u0)
            or (_u0["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL):
        if _pv == "locked":
            return render_template(
                "tracker_public.html",
                public_mode=False,
                cta_url=url_for("desbloquear_platinum"),
                watch_url=url_for("masterclass"),
                upsell_cta_url=url_for("desbloquear_platinum"),
            )
        return render_template(
            "tracker_locked.html",
            upsell_cta_url=url_for("desbloquear_platinum"),
            past_due_mode=True,
            update_card_url=url_for("desbloquear_platinum"),
        )
    # El admin no usa tracker personal: ve el oficial con tickets (2026-10-03,
    # orden de Alex). Su "Alex Owner Results" estaba vacío y no tenía sentido.
    if is_admin_for(current_user()):
        return redirect(url_for("resultados"))
    # Toggle Value Plays solo para Alex (2026-10-11, pedido por Alex)
    user = current_user()
    is_alex = (user["email"] or "").strip().lower() == ALEX_MEMBER_EMAIL
    # Tracker personal SOLO para miembros que pagan (2026-10-15, orden
    # de Alex): quien no paga ve la pagina de venta con candado. El
    # admin ya salio arriba y la cuenta personal de Alex no se toca.
    # La cuenta de MIEMBRO de Alex obedece la misma bandera que todos
    # (2026-10-15, orden suya): asi puede quitarse el Platinum desde el
    # panel y probar la vista de quien no paga. Su cuenta admin no se toca.
    if not platinum_unlocked_for(user):
        # Dos estados (2026-10-15, pedido por Alex con captura de WGT):
        # (a) TUVO Platinum y se le vencio o le fallo el pago -> aviso
        #     de actualizar la tarjeta (su historial sigue guardado);
        # (b) NUNCA desbloqueo Platinum -> pagina de venta con candado.
        try:
            _vencido = bool(user["stripe_past_due"]) or bool(
                (user["stripe_customer_id"] or "").strip()) or bool(
                (user["stripe_subscription_id"] or "").strip())
        except (KeyError, IndexError, TypeError):
            _vencido = False
        _pay_url = None
        if _vencido:
            try:
                _pay_url = (_past_due_billing_info(user) or {}).get("pay_url")
            except Exception:
                _pay_url = None
        if _vencido:
            # Tuvo Platinum y el pago falló/venció la sub: aviso de
            # actualizar la tarjeta (su historial sigue guardado).
            return render_template(
                "tracker_locked.html",
                upsell_cta_url=url_for("desbloquear_platinum"),
                past_due_mode=True,
                update_card_url=url_for("desbloquear_platinum"),
            )
        # Sin Platinum activo (nunca lo activó o se le acabó el mes
        # gratis): la página de venta con candado WHAT MEMBERS GET,
        # la misma que ve el visitante sin sesión (2026-10-19).
        return render_template(
            "tracker_public.html",
            public_mode=False,
            cta_url=url_for("desbloquear_platinum"),
            watch_url=url_for("masterclass"),
            upsell_cta_url=url_for("desbloquear_platinum"),
        )
    if is_alex and request.args.get("view") == "values":
        return values_page()
    ctx = _tracker_context(user)
    ctx["is_alex"] = is_alex
    ctx["view"] = request.args.get("view", "personal")
    return render_template("dashboard.html", **ctx)


@app.route("/tracker/plays")
def tracker_plays():
    """Lista completa de jugadas trackeadas del miembro (2026-10-19,
    refinamiento de Alex): la vista principal de /tracker queda compacta
    en una sola pantalla y la lista vive en esta vista propia, enlazada
    desde abajo del tracker. Misma puerta que /tracker: sin sesion, el
    admin o quien no tenga Platinum activo caen a /tracker, que muestra
    la pagina que corresponda (venta con candado u oficial). Las acciones
    de calificacion (set_result/untrack) usan back(), asi que desde aqui
    regresan solas a esta vista."""
    user = current_user()
    if user is None or is_admin_for(user) or not platinum_unlocked_for(user):
        return redirect(url_for("tracker"))
    ctx = _tracker_context(user)
    return render_template("tracker_plays.html", **ctx)


_SPORT_ICONS = {"NFL": "🏈", "NCAAF": "🏈", "MLB": "⚾", "NHL": "🏒",
                "NBA": "🏀"}
_ODDS_KEY_LEAGUE = {"americanfootball_nfl": "NFL",
                    "americanfootball_ncaaf": "NCAAF",
                    "baseball_mlb": "MLB", "icehockey_nhl": "NHL",
                    "basketball_nba": "NBA"}


def _sport_icon(t, off):
    """Icono del deporte de una fila del tracker (2026-10-14, bug visto
    por Alex: el fallback era SIEMPRE 🏈, hasta en jugadas NHL/MLB).
    Orden: liga de la jugada oficial enlazada → clave de deporte guardada
    en la fila → derivación por el pick. Sin liga conocida: 🎯 neutro."""
    liga = None
    if off:
        liga = _league_of_archive_play(off)
    if not liga:
        liga = _ODDS_KEY_LEAGUE.get((t.get("liga") or "").strip())
    if not liga:
        liga = _league_of_archive_play({"pick": t.get("pick") or ""})
    return _SPORT_ICONS.get(liga or "", "🎯")


def _tracker_context(user):
    """Contexto del tracker personal para un usuario dado (fila de users).

    Extraído de tracker() el 2026-10-05 para reutilizarlo en la vista previa
    del rediseño estilo 'My Action' sin duplicar la lógica.
    """
    db = get_db()
    uid = user["id"]
    auto_grado_tracked(db, user)
    tracked = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC",
        (uid,),
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
    # Enlace por play_id (2026-10-14, bug visto por Alex: "no se están
    # publicando los tickets ahí"): la fila trackeada guarda el pick CORTO
    # de plays.json ("Carolina Hurricanes ML") y el archivo el LARGO
    # ("...vs Washington Capitals"), así que el match por (fecha, pick)
    # fallaba y la fila quedaba sin ticket ni marcador. El play_id SÍ es
    # el mismo en los tres lados: se busca primero por id (archivo y, para
    # las de hoy aún sin liquidar, plays.json publicado) y el match por
    # texto queda solo como respaldo.
    oficiales_id = {}
    for d in load_archive():
        for j in d.get("jugadas", []) or []:
            if j.get("id"):
                oficiales_id[j["id"]] = j
    for p in load_plays():
        if p.get("id") and p["id"] not in oficiales_id:
            oficiales_id[p["id"]] = p
    rows = []
    for t in tracked:
        f = (t["fecha"] or "").strip()
        off = (oficiales_id.get(t["play_id"])
               or oficiales.get((f, (t["pick"] or "").strip().lower()), {}))
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
            "sport_icon": _sport_icon(t, off),
        })
    # Récord Platinum real del archivo oficial (para el upsell del tracker).
    _pw = _pl = 0
    for _d in load_archive():
        for _j in _d.get("jugadas", []) or []:
            if _j.get("resultado") not in ("WON", "LOST") or _j.get("bloqueada"):
                continue
            if (_j.get("nivel") or "").upper() in ("ELITE", "PLATINUM"):
                if _j["resultado"] == "WON":
                    _pw += 1
                else:
                    _pl += 1
    return dict(
        plays=[] if card_pendiente else load_plays(),
        card_pendiente=card_pendiente,
        rows=rows,
        plat_w=_pw,
        plat_l=_pl,
        tracked_ids=tracked_ids,
        stats=stats,
        nombre=user["nombre"] if user else "",
        bankroll=user["bankroll"] if user else None,
        stake_mode=user["stake_mode"] if user else "units",
        platinum_unlocked=platinum_unlocked_for(user),
        res=program_stats(),
        recientes=recientes_oficiales(),
        recientes_value=recientes_value(),
        streak=program_streak(),
        resumen_ayer=resumen_ayer(),
        es_admin=is_admin_for(user),
    )


@app.route("/preview/my-action-8f3k2")
@login_required
def preview_my_action():
    """VISTA PREVIA TEMPORAL (2026-10-05, pedido por Alex): rediseño del
    tracker personal estilo 'My Action' de Action Network, con las
    herramientas del programa. URL sin enlaces en la app; muestra siempre
    el tracker del miembro de Alex para que él lo apruebe desde cualquiera
    de sus dos teléfonos. Se elimina al aprobar o rechazar el diseño."""
    db = get_db()
    alex = db.execute(
        "SELECT * FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL.lower(),)
    ).fetchone()
    if not alex:
        return "Preview no disponible", 404
    ctx = _tracker_context(alex)
    rows = ctx["rows"]
    # Desglose por nivel para la pestaña Stats
    por_nivel = {}
    for r in rows:
        niv = "Platinum" if (r["nivel"] or "").upper() in ("ELITE", "PLATINUM") else "Gold"
        d = por_nivel.setdefault(niv, {"w": 0, "l": 0, "profit": 0.0})
        if r["resultado"] == "W":
            d["w"] += 1
        elif r["resultado"] == "L":
            d["l"] += 1
        d["profit"] += float(r["profit"] or 0)
    # Curva de profit acumulado (cronológico) para "My Stats"
    cron = sorted([r for r in rows if r["resultado"] in ("W", "L")],
                  key=lambda r: (r["fecha"], r["id"]))
    acc, pts = 0.0, []
    for r in cron:
        acc += float(r["profit"] or 0)
        pts.append(round(acc, 2))
    br = float(ctx.get("bankroll") or 0)
    return render_template(
        "my_action_preview.html", **ctx, por_nivel=por_nivel,
        curve_pts=pts, u1=round(br * 0.01, 2), u06=round(br * 0.006, 2),
    )


@app.route("/free-plays")
@login_required
def free_plays():
    """DESACTIVADO (2026-10-04, orden de Alex): tracker de prueba eliminado.
    Quedan: récord oficial, tracker personal y tracker VALUE."""
    from flask import redirect, url_for
    return redirect(url_for("home"))


@app.route("/admin/free-plays/clear", methods=["POST"])
@login_required
def admin_free_plays_clear():
    """Borra el tracker "Gold" (solo admin). No afecta tracked_plays
    ni el récord oficial del programa."""
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        return redirect(url_for("home"))
    db = get_db()
    db.execute("DELETE FROM free_plays")
    db.commit()
    return redirect(url_for("free_plays"))


@app.route("/api/free-plays/add", methods=["POST"])
def api_free_plays_add():
    """Registra jugadas en el tracker "Gold". Protegido con header
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
    # Export del tracker personal: mismo candado de membresia (2026-10-15).
    _cu = current_user()
    if not is_admin_for(_cu) and not platinum_unlocked_for(_cu):
        return redirect(url_for("tracker"))
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

    liquidadas = stats["wins"] + stats["losses"]
    wr = (stats["wins"] / liquidadas * 100) if liquidadas else 0
    plays_html = []
    for t in filtradas:
        f = (t["fecha"] or "").strip()
        off = oficiales.get((f, (t["pick"] or "").strip().lower()), {})
        res = t["resultado"] or ""
        nivel = (t["nivel"] or "").upper()
        nivel_txt = "PLATINUM" if nivel in ("ELITE", "PLATINUM") else ("GOLD" if nivel == "GOLD" else nivel)
        try:
            fecha_corta = datetime.strptime(f, "%Y-%m-%d").strftime("%b %d") if f else "—"
        except (ValueError, TypeError):
            fecha_corta = f or "—"
        plays_html.append({
            "pick": t["pick"] or "",
            "fecha": fecha_corta,
            "cuota": fmt_odds(t["cuota"]),
            "nivel": nivel,
            "nivel_txt": nivel_txt,
            "stake": t["stake_monto"] or 0,
            "resultado": res if res in ("W", "L") else "P",
            "resultado_txt": {"W": "Won", "L": "Lost"}.get(res, "Pending"),
            "profit": play_profit_dollars(t) if res in ("W", "L") else None,
            "casa": off.get("casa") or "Novig",
        })
    etiqueta = {"7": "7d", "30": "30d", "365": "12m", "mtd": "mtd", "all": "all"}.get(
        rng, f"{desde.isoformat()}_{hasta.isoformat()}" if desde else "all")
    fname = f"tracker-{etiqueta}-{hoy.isoformat()}.html"
    html = render_template("tracker_export.html",
        nombre_rango=nombre_rango,
        generado=datetime.now(TZ).strftime("%Y-%m-%d %H:%M ET"),
        profit=stats["net_dollars"],
        liquidadas=liquidadas,
        win_rate=wr,
        wins=stats["wins"],
        losses=stats["losses"],
        staked=stats["risked"],
        roi=stats["roi"],
        plays=plays_html)
    return Response(html, mimetype="text/html",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})



@app.route("/api/admin/autotrack", methods=["POST"])
def api_admin_autotrack():
    import json as _json, os
    from datetime import datetime
    key_ok = request.headers.get("X-Push-Key") == os.environ.get("PUSH_TRIGGER_KEY", "")
    if not key_ok:
        try:
            kf = os.path.join(os.path.dirname(__file__), "..", "goals",
                              "programa-de-apuestas-deportivas", "hidden_files", "push-keys.env")
            _key = ""
            with open(kf) as _f:
                for _line in _f:
                    if _line.strip().startswith("PUSH_TRIGGER_KEY="):
                        _key = _line.strip().split("=", 1)[1]
                        break
            if request.headers.get("X-Push-Key") != _key:
                return jsonify({"error": "unauthorized"}), 401
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    play_id = data.get("play_id", "")
    email = data.get("email", "pena.manuel@myyahoo.com").strip().lower()
    try:
        db = get_db()
        user = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (email,)).fetchone()
        if not user:
            return jsonify({"error": "user not found"}), 404
        user_id = user["id"] if isinstance(user, dict) else user[0]
        play = next((p for p in load_plays() if p.get("id") == play_id), None)
        if not play:
            return jsonify({"error": "play not found"}), 404
        existing = db.execute(
            "SELECT id FROM tracked_plays WHERE user_id=? AND play_id=?",
            (user_id, play_id)).fetchone()
        if existing:
            return jsonify({"ok": True, "already": True})
        hoy = datetime.now().strftime("%Y-%m-%d")
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, play_id, hoy, play.get("nivel"), play.get("pick"),
             play.get("cuota"), play.get("unidades", 1), play.get("stake", 50),
             _edge_num(play.get("edge")), "PENDING", datetime.now().isoformat()))
        db.commit()
        return jsonify({"ok": True, "tracked": play_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/track/<play_id>", methods=["GET", "POST"])
@login_required
def track(play_id):
    play = next((p for p in load_plays() if p.get("id") == play_id), None)
    _cu = current_user()
    if play is None and is_admin_for(_cu):
        # Solo el admin puede ver la card pendiente (sin tickets)
        # desde su dashboard (2026-10-03, orden de Alex).
        play = next((p for p in load_pending_plays() if p.get("id") == play_id), None)
    _is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not play:
        if _is_ajax:
            return jsonify({"ok": False, "error": "Play not found."}), 404
        flash("Play not found.", "error")
        return redirect(url_for("home"))
    # Tracker personal SOLO con membresia pagando (2026-10-15, orden
    # de Alex): sin pago no se puede trackear — primero la membresia.
    if not is_admin_for(_cu) and not platinum_unlocked_for(_cu):
        if _is_ajax:
            return jsonify({"ok": False, "error": "Your personal Tracker comes with a paid membership. Tap Tracker below to see how it works."}), 403
        flash("Your personal Tracker comes with a paid membership.", "warn")
        return redirect(url_for("tracker"))
    # Track con o sin ticket (2026-10-10, orden de Alex: revierte la regla
    # anti-trampa del 2026-10-03; el botón "+ Track" ahora es visible siempre).
    # La Elite bloqueada no se puede trackear: no revela nada.
    if play.get("nivel") in ("PLATINUM", "ELITE") and not platinum_unlocked_for(_cu):
        if _is_ajax:
            return jsonify({"ok": False, "error": "The Platinum play is locked. Unlock it to track it."}), 403
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
                session["user_id"], play["id"], fecha_de_play(play),
                play.get("nivel", ""), play.get("pick", ""), int(play.get("cuota", 0)),
                float(play.get("stake_unidades", 0)),
                float(stake_personalizado(play, _cu)),
                _edge_num(play.get("edge")), now_iso(),
            ),
        )
        db.commit()
        if _is_ajax:
            return jsonify({"ok": True})
        flash("Play added to your tracker.", "ok")
    except INTEGRITY_ERRORS:
        if _is_ajax:
            return jsonify({"ok": True, "already": True})
        flash("That play is already in your tracker.", "warn")
    except Exception:
        # Blindaje 2026-10-12: cualquier otro error de BD devuelve JSON al
        # AJAX (antes un 500 HTML y el miembro solo veía el aviso genérico).
        try:
            db.rollback()
        except Exception:
            pass
        if _is_ajax:
            return jsonify({"ok": False, "error": "Could not track this play."}), 500
        flash("Could not track this play.", "error")
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
    from flask import request
    # Intro promocional de 40s la primera vez (pedido Alex 2026-10-06).
    if not request.args.get("start"):
        return render_template("masterclass_intro.html")
    return render_template(
        "masterclass.html",
        leccion=load_masterclass(),
        archivo=load_masterclass_archivo(),
    )


# Guías educativas para principiantes (pedido por Alex 2026-10-06).
@app.route("/guides/moneyline")
@login_required
def guide_moneyline():
    return render_template("guides/moneyline.html")


@app.route("/guides/spread")
@login_required
def guide_spread():
    return render_template("guides/spread.html")


@app.route("/guides/total")
@login_required
def guide_total():
    return render_template("guides/total.html")


@app.route("/guides/american-odds")
@login_required
def guide_american_odds():
    return render_template("guides/american-odds.html")


@app.route("/guides/vig")
@login_required
def guide_vig():
    return render_template("guides/vig.html")


@app.route("/guides/reading-card")
@login_required
def guide_reading_card():
    return render_template("guides/reading-card.html")


@app.route("/guides/glossary")
@login_required
def guide_glossary():
    return render_template("guides/glossary.html")


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


@app.route("/records")
@login_required
def records():
    """Página de récord documentado estilo WGT (pedido Alex 2026-10-06 con capturas):
    hero + cajas de stats (oficiales, Platinum, Gold) + lista completa de jugadas liquidadas."""
    db = get_db()
    record_checkin(db, session["user_id"])
    # RETIRADO 2026-10-12 (orden de Alex): solo quedan el tracker EJECUTIVO
    # del programa y el de VALUES — /records redirige al ejecutivo.
    return redirect(url_for("official_tracker"))

    def _units(j):
        """Unidades ganadas/perdidas de una jugada liquidada."""
        try:
            stake = float(j.get("stake_monto") or 0)
            u = float(j.get("stake_unidades") or 0)
            profit = float(j.get("profit") or 0)
            if u > 0 and stake > 0:
                return profit / (stake / u)
            return 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    stats = {"W": 0, "L": 0, "units": 0.0}
    by_level = {"PLATINUM": {"W": 0, "L": 0, "units": 0.0},
                "GOLD": {"W": 0, "L": 0, "units": 0.0}}
    graded = []
    for d in load_archive():
        fecha = d.get("fecha", "")
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") not in ("WON", "LOST") or j.get("bloqueada"):
                continue
            nv = (j.get("nivel") or "").upper()
            if nv not in ("ELITE", "PLATINUM", "GOLD"):
                continue
            disp = "PLATINUM" if nv in ("ELITE", "PLATINUM") else "GOLD"
            un = _units(j)
            won = j["resultado"] == "WON"
            stats["W" if won else "L"] += 1
            stats["units"] += un
            by_level[disp]["W" if won else "L"] += 1
            by_level[disp]["units"] += un
            graded.append({
                "pick": j.get("pick", ""), "cuota": j.get("cuota"),
                "nivel": disp, "liga": j.get("liga", ""), "fecha": fecha,
                "won": won, "units": un,
                "comprobante": j.get("comprobante", ""),
                "id": j.get("id", ""),
                "abbr": j.get("abbr", ""), "bettype": j.get("bettype", ""),
            })
    graded.sort(key=lambda r: r["fecha"], reverse=True)
    total = stats["W"] + stats["L"]
    wr = (100.0 * stats["W"] / total) if total else 0.0
    for _lv in by_level.values():
        _t = _lv["W"] + _lv["L"]
        _lv["wr"] = (100.0 * _lv["W"] / _t) if _t else 0.0
    return render_template("records.html", stats=stats, by_level=by_level,
                           graded=graded, total=total, win_rate=wr)


@app.route("/about")
def about():
    """Nuestra historia — la promesa (pedido por Alex 2026-10-06).
    Página pública, no requiere login."""
    return render_template("about.html")


def _desbloquear_board():
    """Datos REALES del programa para la página de desbloqueo (2026-10-19).

    Récord oficial con el MISMO cálculo del Official Tracker
    (official_tracker_full), teasers de las últimas jugadas liquidadas y
    los tickets GANADORES recientes con su foto — todo desde
    data/archive.json. Si algo falla, devuelve vacío y la página se
    renderiza igual, sin esas piezas (nunca números inventados).
    """
    board = {"w": 0, "l": 0, "total": 0, "win_rate": 0.0,
             "record": "0–0", "teasers": [], "tickets": []}
    try:
        g = official_tracker_full().get("general") or {}
        board.update({"w": g.get("w", 0), "l": g.get("l", 0),
                      "total": g.get("n", 0),
                      "win_rate": g.get("win_rate", 0.0),
                      "record": g.get("record", "0–0")})
        dias = sorted(load_archive(), key=lambda d: d.get("fecha") or "")
        settled = []
        for d in dias:
            for j in d.get("jugadas", []) or []:
                if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada"):
                    settled.append((d.get("fecha") or "", j))
        newest = list(reversed(settled))

        def _wl(items):
            w = sum(1 for _, j in items if j["resultado"] == "WON")
            return w, len(items) - w

        teasers = []
        if newest:
            liga_top = _league_of_archive_play(newest[0][1])
            if liga_top:
                de_liga = [it for it in newest
                           if _league_of_archive_play(it[1]) == liga_top][:5]
                if len(de_liga) >= 3:
                    w, l = _wl(de_liga)
                    teasers.append({
                        "texto": f"{w}–{l} in the last {len(de_liga)} {liga_top} plays",
                        "sub": "Official plays · tap for the graded plays"})
            ult10 = newest[:10]
            if len(ult10) >= 5:
                w, l = _wl(ult10)
                teasers.append({
                    "texto": f"{w}–{l} in the last {len(ult10)} official plays",
                    "sub": "Every league · tap for the graded plays"})
            ult5 = newest[:5]
            if len(ult5) >= 3 and len(teasers) < 3:
                w, l = _wl(ult5)
                teasers.append({
                    "texto": f"{w}–{l} in the last {len(ult5)} official plays",
                    "sub": "Most recent · tap for the graded plays"})
        board["teasers"] = teasers[:3]
        tickets = []
        for fecha, j in newest:
            if j.get("resultado") == "WON" and j.get("comprobante"):
                p = float(j.get("profit") or 0)
                try:
                    f_txt = datetime.strptime(fecha, "%Y-%m-%d").strftime("%b %d")
                except Exception:
                    f_txt = fecha
                tickets.append({
                    "img": j["comprobante"],
                    "pick": j.get("pick") or "",
                    "fecha": f_txt,
                    "profit": ("+$" + (f"{p:,.0f}" if p == int(p) else f"{p:,.2f}")),
                })
            if len(tickets) >= 6:
                break
        board["tickets"] = tickets
    except Exception:
        pass
    return board


def _checkout_embed_confirmado(session_id, user):
    """True si la Checkout Session `session_id` quedó COMPLETA y es del
    miembro en sesión. Solo pinta el estado de éxito al volver del pago
    incrustado — la activación real la hace el webhook de siempre."""
    if not (session_id and user and STRIPE_SECRET_KEY):
        return False
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        cs = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return False
    if (cs.get("status") or "") != "complete":
        return False
    meta = cs.get("metadata") or {}
    try:
        return str(meta.get("member_id") or "") == str(user["id"])
    except (KeyError, IndexError, TypeError):
        return False


@app.route("/desbloquear-platinum")
def desbloquear_platinum():
    # PUBLICA por diseno (2026-10-20, orden de Alex): el boton de los
    # correos debe aterrizar en la oferta sin sesion — los navegadores
    # internos de los correos no traen la cookie y el rebote al login se
    # veia como link roto. Solo la OFERTA es publica: crear el cobro
    # sigue exigiendo sesion (endpoint con @login_required) y la
    # activacion la sigue haciendo el webhook tras un pago real.
    db = get_db()
    if session.get("user_id"):
        record_checkin(db, session["user_id"])
    user = current_user()
    try:
        past_due = bool(user and user["stripe_past_due"])
    except (KeyError, IndexError, TypeError):
        past_due = False
    # Vuelta del checkout incrustado (?checkout=ok&session_id=...): el éxito
    # solo se muestra con la sesión de Stripe verificada como completa y
    # propia; el webhook activa en paralelo y la página tolera su retraso.
    checkout_ok = False
    if request.args.get("checkout") == "ok" and request.args.get("session_id"):
        checkout_ok = _checkout_embed_confirmado(
            request.args.get("session_id"), user)
    return render_template(
        "desbloquear.html",
        ya_desbloqueada=platinum_unlocked_for(user),
        past_due=past_due,
        stripe_url=STRIPE_PLATINUM_URL,
        stripe_pk=STRIPE_PUBLISHABLE_KEY,
        stripe_embed_ok=bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY),
        checkout_ok=checkout_ok,
        board=_desbloquear_board(),
        anonimo=(user is None),
        user_email=(user["email"] if user else ""),
        user_nombre=(user["nombre"] if user else ""),
    )


@app.route("/api/desbloquear/checkout-session", methods=["POST"])
@login_required
def desbloquear_checkout_session():
    """Checkout INCRUSTADO de la oferta $1 (espejo WGT, 2026-10-19): crea
    la Checkout Session en modo embedded y devuelve su client_secret; el
    formulario de Stripe se monta DENTRO de /desbloquear-platinum y el
    miembro paga sin salir del sitio. Cobro: $1 hoy (precio one-time) +
    suscripción de $15.99/semana con 7 días de trial (el primer cobro
    semanal cae al día 8) — los mismos precios del Payment Link hospedado,
    que queda como respaldo. La activación la hace el webhook
    checkout.session.completed de siempre (match por email)."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "stripe not configured"}), 503
    user = current_user()
    if user is None:
        return jsonify({"error": "login required"}), 401
    email = (user["email"] or "").strip()
    try:
        customer_id = (user["stripe_customer_id"] or "").strip()
    except (KeyError, IndexError, TypeError):
        customer_id = ""
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        params = {
            # El SDK del servidor (stripe 12.5.0) fija la API
            # 2025-08-27.basil: ahí el modo incrustado se llama "embedded"
            # (en la API más nueva de la cuenta es "embedded_page").
            "ui_mode": "embedded",
            "mode": "subscription",
            "line_items": [
                {"price": PLATINUM_PRICE_PRIMERA_SEMANA, "quantity": 1},
                {"price": PLATINUM_PRICE_SEMANAL, "quantity": 1},
            ],
            "subscription_data": {"trial_period_days": 7},
            # Solo Card + Apple Pay (orden de Alex 2026-10-20, espejo
            # del checkout de WGT): en la API basil que fija el SDK,
            # payment_method_types=['card'] deja fuera los metodos
            # dinamicos extra (Klarna, Cash App, Amazon Pay y Link) y
            # Apple Pay viaja dentro de 'card' (los dominios
            # thesharpteam.bet ya estan registrados en Stripe). Link
            # queda apagado del todo con wallet_options: el formulario
            # abre directo en el pago, sin su aviso de codigo.
            "payment_method_types": ["card"],
            "wallet_options": {"link": {"display": "never"}},
            "metadata": {
                "member_email": email,
                "member_id": str(user["id"]),
                "source": "desbloquear-embedded",
            },
            "return_url": (
                url_for("desbloquear_platinum", _external=True)
                + "?checkout=ok&session_id={CHECKOUT_SESSION_ID}"
            ),
        }
        if customer_id:
            params["customer"] = customer_id
        else:
            params["customer_email"] = email
        try:
            cs = stripe.checkout.Session.create(**params)
        except Exception:
            # Si la API rechazara algun dia los parametros de metodos
            # de pago, se reintenta con los parametros de siempre: el
            # checkout nunca se cae por este ajuste.
            params.pop("payment_method_types", None)
            params.pop("wallet_options", None)
            cs = stripe.checkout.Session.create(**params)
    except Exception:
        return jsonify({"error": "could not create checkout session"}), 502
    return jsonify({"clientSecret": cs.get("client_secret") or ""})


@app.route("/admin/api/members-emails")
def admin_api_members_emails():
    """Lista de miembros para emails personalizados (2026-10-07, pedido por Alex):
    los emails de motivación salen con sección de upsell solo para no-Platinum.
    Retorna JSON: [{email, nombre, platinum, phone, sms_optin, lang, platinum_trial_ends}].
    Auth: sesión admin O header X-Admin-Key == ADMIN_API_KEY (para los crons)."""
    api_key = os.environ.get("ADMIN_API_KEY", "")
    header_key = request.headers.get("X-Admin-Key", "")
    is_cron = api_key and header_key and hmac.compare_digest(header_key, api_key)
    try:
        is_admin = is_admin_for(current_user())
    except Exception:
        is_admin = False
    if not (is_cron or is_admin):
        return jsonify({"error": "admin only"}), 403
    db = get_db()
    _barrer_trials_vencidos(db)
    rows = db.execute(
        "SELECT email, nombre, platinum_unlocked, phone, sms_optin, lang, platinum_trial_ends FROM users WHERE email IS NOT NULL AND email != '' AND email_verified = 1"
    ).fetchall()
    return jsonify([
        {"email": r["email"], "nombre": r["nombre"] or "there",
         "platinum": bool(r["platinum_unlocked"]),
         "phone": r["phone"] or "", "sms_optin": bool(r["sms_optin"]),
         "lang": r["lang"] or "en", "platinum_trial_ends": r["platinum_trial_ends"]}
        for r in rows
    ])


@app.route("/admin/miembros/platinum", methods=["POST"])
@login_required
def admin_toggle_platinum():
    """Activa o quita el acceso Elite de un miembro (solo Alex).

    Stripe (payment link) no avisa solo a la app, así que cuando llega
    la notificación de pago Alex activa el acceso aquí con un toque.
    """
    db = get_db()
    if not _admin_o_cuenta_alex(current_user()):
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
        target = db.execute("SELECT email, nombre FROM users WHERE id = ?", (user_id,)).fetchone()
        if target:
            if nuevo:
                log_notificacion(db, "platinum", f"Platinum activated: {target['nombre']}", target["email"])
            else:
                log_notificacion(db, "platinum", f"Platinum removed: {target['nombre']}", target["email"])
        flash(
            "Platinum access activated." if nuevo else "Platinum access deactivated.",
            "ok",
        )
    return redirect(url_for("admin_miembros"))


def _admin_api_autorizado():
    """True si hay sesión admin O header X-Admin-Key válido (para los crons/scripts)."""
    api_key = os.environ.get("ADMIN_API_KEY", "")
    header_key = request.headers.get("X-Admin-Key", "")
    if api_key and header_key and hmac.compare_digest(header_key, api_key):
        return True
    try:
        return bool(is_admin_for(current_user()))
    except Exception:
        return False


@app.route("/admin/api/stake-mode", methods=["POST"])
def admin_api_stake_mode():
    """Fija el modo de sizing de un miembro (units | kelly) por API admin.

    2026-10-13: la cuenta de miembro de Alex quedó en modo kelly (activado
    por accidente el 2026-10-03) y su card mostraba un monto distinto al del
    ticket oficial ($189 vs $50). Este endpoint permite corregirlo por API
    sin depender de la sesión del miembro. Auth: sesión admin O X-Admin-Key.
    """
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    mode = (data.get("mode") or request.form.get("mode") or "").strip().lower()
    if mode not in ("units", "kelly"):
        return jsonify({"error": "invalid mode"}), 400
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET stake_mode = ? WHERE id = ?", (mode, row["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email, "stake_mode": mode})


@app.route("/admin/api/members-delete", methods=["POST"])
def admin_api_members_delete():
    """Elimina una cuenta de miembro por API admin (2026-10-13, pedido por Alex:
    borrar su cuenta de prueba sin depender de la sesión del panel).
    Mismas protecciones que la ruta del panel: nunca una cuenta admin ni la
    cuenta de sistema VALUE. Auth: sesión admin O header X-Admin-Key."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    if is_admin_for(target) or email == "system@value.local":
        return jsonify({"error": "protected account"}), 403
    db.execute("DELETE FROM tracked_plays WHERE user_id = ?", (target["id"],))
    db.execute("DELETE FROM checkins WHERE user_id = ?", (target["id"],))
    db.execute("DELETE FROM push_subscriptions WHERE member_id = ?", (target["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (target["id"],))
    db.commit()
    log_notificacion(db, "miembro", f"Member deleted: {email}", "")
    return jsonify({"ok": True, "deleted": email})


@app.route("/admin/api/members-set-admin", methods=["POST"])
def admin_api_members_set_admin():
    """Quita o pone la bandera is_admin de una cuenta por API admin
    (2026-10-15, pedido por Alex: su cuenta de miembro queda como
    miembro normal, sin admin; el admin vive solo en su cuenta admin).
    Auth: sesión admin O header X-Admin-Key. Protecciones: nunca
    degradar la cuenta admin real (thesharpteam8@gmail.com) ni dejar
    el sistema sin ningún admin."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    val = data.get("is_admin")
    if not email or val is None:
        return jsonify({"error": "email and is_admin required"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    nuevo = 1 if val in (True, 1, "1", "true") else 0
    if nuevo == 0 and is_admin_for(target):
        if email == "thesharpteam8@gmail.com":
            return jsonify({"error": "protected account"}), 403
        fila = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()
        n_admin = fila["n"] if isinstance(fila, dict) else fila[0]
        if n_admin <= 1:
            return jsonify({"error": "cannot remove the last admin"}), 403
    db.execute("UPDATE users SET is_admin = ? WHERE id = ?",
               (nuevo, target["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email, "is_admin": nuevo})


@app.route("/admin/api/members-set-name", methods=["POST"])
def admin_api_members_set_name():
    """Corrige el nombre (campo `nombre`) de un miembro por API admin
    (2026-10-15, pedido por Alex: su hermano se registró poniendo su
    número de teléfono en vez de su nombre y hubo que corregirlo sin
    depender de la sesión del miembro). Auth: sesión admin O header
    X-Admin-Key (mismo patrón que members-set-admin)."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    nombre = (data.get("nombre") or data.get("name") or
              request.form.get("nombre") or "").strip()[:120]
    if not email or not nombre:
        return jsonify({"error": "email and nombre required"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?",
                        (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET nombre = ? WHERE id = ?",
               (nombre, target["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email, "nombre": nombre})


@app.route("/admin/api/members-set-lang", methods=["POST"])
def admin_api_members_set_lang():
    """Fija el idioma de la interfaz de un miembro ('en' o 'es')
    (2026-10-16, pedido por Alex: la cuenta de su hermano Johan queda
    en español porque no entiende inglés). Con lang='es', base_v2 le
    muestra el botón de idioma y el traductor se activa solo al
    entrar. Auth: sesión admin O header X-Admin-Key (mismo patrón
    que members-set-name)."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    lang = (data.get("lang") or request.form.get("lang") or "").strip().lower()
    if not email or lang not in ("en", "es"):
        return jsonify({"error": "email and lang (en|es) required"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?",
                        (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET lang = ? WHERE id = ?",
               (lang, target["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email, "lang": lang})


@app.route("/admin/api/members-reset-link", methods=["POST"])
def admin_api_members_reset_link():
    """Genera un enlace de un solo uso para que un miembro cree una
    contraseña nueva (2026-10-16, pedido por Alex para su hermano
    Johan, que olvidó la suya y la plataforma no tenía flujo de
    recuperación). La URL se devuelve SOLO en la respuesta de esta
    API: viaja únicamente en el email del miembro, nunca en el chat
    ni en logs. Auth: sesión admin O header X-Admin-Key."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?",
                        (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    token = secrets.token_urlsafe(32)
    vence = (datetime.now() + timedelta(hours=24)).isoformat()
    db.execute("UPDATE users SET reset_token = ?, reset_expires = ?"
               " WHERE id = ?", (token, vence, target["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email,
                    "url": f"https://www.thesharpteam.bet/reset/{token}",
                    "expira": vence})


@app.route("/admin/api/platinum-trial-activate-all", methods=["POST"])
def admin_api_platinum_trial_activate_all():
    """Activa el mes gratis de Platinum (30 días, reloj interno) a todos
    los miembros sin Platinum y sin suscripción de Stripe (orden de Alex
    2026-10-16: todos activos; al vencer los 30 días se apaga solo).
    NO toca: cuentas admin, la cuenta del sistema, miembros con sub de
    Stripe (manda Stripe) ni grants ya activos sin reloj (manuales)."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    db = get_db()
    vence = (datetime.now() + timedelta(days=30)).isoformat()
    cur = db.execute(
        "UPDATE users SET platinum_unlocked = 1, platinum_trial_ends = ?, "
        "telegram_unban_pending = CASE WHEN telegram_user_id IS NOT NULL "
        "THEN 1 ELSE telegram_unban_pending END "
        "WHERE platinum_unlocked = 0 AND is_admin = 0 "
        "AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '') "
        "AND email != 'system@value.local'",
        (vence,),
    )
    db.commit()
    return jsonify({"ok": True, "activados": cur.rowcount, "vence": vence})


@app.route("/admin/api/members-set-stakes", methods=["POST"])
def admin_api_members_set_stakes():
    """Fija los montos por jugada de un miembro (stake_fijo_elite /
    stake_fijo_gold), que anulan la fórmula del 1% de su bankroll
    (2026-10-16, pedido por Alex para su hermano Johan: Platinum $25
    y Gold $15 fijos). Auth: sesión admin O header X-Admin-Key
    (mismo patrón que los demás members-set-*)."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    try:
        plat = float(data.get("platinum"))
        gold = float(data.get("gold"))
    except (TypeError, ValueError):
        return jsonify({"error": "platinum and gold amounts required"}), 400
    if not email or plat < 0 or gold < 0:
        return jsonify({"error": "invalid amounts"}), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE email = ?",
                        (email,)).fetchone()
    if not target:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET stake_fijo_elite = ?, stake_fijo_gold = ?"
               " WHERE id = ?", (plat, gold, target["id"]))
    db.commit()
    return jsonify({"ok": True, "email": email,
                    "stake_fijo_elite": plat, "stake_fijo_gold": gold})


@app.route("/admin/test-clear-past-due", methods=["POST"])
def admin_test_clear_past_due():
    """TEMPORAL para pruebas de Alex: quita SOLO el flag stripe_past_due,
    sin tocar el resto del estado (para volver al estado previo a la prueba).
    Auth: sesión admin O header X-Admin-Key."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    db = get_db()
    email = (request.form.get("email") or "").strip().lower()
    row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET stripe_past_due = 0 WHERE id = ?", (row["id"],))
    db.commit()
    return jsonify({"ok": True, "email": email})


@app.route("/admin/test-simulate-past-due", methods=["POST"])
def admin_test_simulate_past_due():
    """TEMPORAL para pruebas de Alex: simula pago fallido (stripe_past_due=1).
    Auth: sesión admin O header X-Admin-Key (para activar por API).
    Para quitarlo, usar test-simulate-reactivate."""
    if not _admin_api_autorizado():
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    db = get_db()
    email = request.form.get("email", "").strip().lower()
    row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        flash("Member not found.", "error")
    else:
        db.execute("UPDATE users SET stripe_past_due = 1 WHERE id = ?", (row["id"],))
        db.commit()
        flash(f"Simulated failed payment for {email}: past-due ON.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/test-simulate-cancel", methods=["POST"])
@login_required
def admin_test_simulate_cancel():
    """TEMPORAL para pruebas de Alex: simula lo que hace el webhook de Stripe
    al cancelar (customer.subscription.deleted). Solo admin."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
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
        flash(f"Simulated cancellation for {email}: Platinum off, ban pending.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/test-simulate-reactivate", methods=["POST"])
@login_required
def admin_test_simulate_reactivate():
    """TEMPORAL para pruebas de Alex: simula lo que hace el webhook de Stripe
    al reactivar/pagar (invoice.paid). Solo admin."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
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
            "telegram_ban_pending = 0, stripe_past_due = 0, cancel_requested_at = NULL WHERE id = ?",
            (row["id"],),
        )
        db.commit()
        flash(f"Simulated reactivation for {email}: Platinum on, past-due cleared, unban pending.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/eliminar", methods=["POST"])
@login_required
def admin_eliminar_miembro():
    """Elimina una cuenta de miembro (solo admin). No permite borrarse a sí mismo ni a otro admin."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
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
    log_notificacion(db, "miembro", f"Member deleted: {target['email']}", "")
    flash(f"Account {target['email']} deleted.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/telegram-ban", methods=["POST"])
@login_required
def admin_telegram_ban():
    """Banea o desbanea a un miembro del canal Sharp Club de Telegram (solo admin).
    Marca el flag pendiente; el cron lo procesa."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    user_id = request.form.get("user_id", type=int)
    action = request.form.get("action", "")  # 'ban' o 'unban'
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin_miembros"))
    if action == "ban":
        db.execute(
            "UPDATE users SET telegram_ban_pending = 1, telegram_unban_pending = 0 WHERE id = ?",
            (user_id,),
        )
        flash(f"{target['email']} marcado para banear de Telegram.", "ok")
    elif action == "unban":
        db.execute(
            "UPDATE users SET telegram_unban_pending = 1, telegram_ban_pending = 0 WHERE id = ?",
            (user_id,),
        )
        flash(f"{target['email']} marcado para desbanear de Telegram.", "ok")
    db.commit()
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/email-directo", methods=["POST"])
@login_required
def admin_email_directo():
    """Envía un email directo a un miembro específico (solo admin)."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    user_id = request.form.get("user_id", type=int)
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not user_id or not subject or not body:
        flash("Faltan datos para enviar el email.", "error")
        return redirect(url_for("admin_miembros"))
    db = get_db()
    target = db.execute("SELECT email, nombre FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin_miembros"))
    try:
        text_body = body  # versión texto plano
        html = f"""<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;background:#ffffff;">
<img src="https://www.thesharpteam.bet/static/img/sharp-team-banner.png" alt="The Sharp Team" style="width:100%;height:auto;display:block;border:0;">
<div style="padding:24px 28px;"><p style="font-size:16px;color:#111;line-height:1.6;">{body}</p>
<p style="font-size:14px;color:#6b7280;margin-top:20px;"><strong>The Sharp Team</strong></p></div></div>"""
        _dispatch_email(target["email"], subject, text_body, html_body=html)
        flash(f"Email enviado a {target['email']}.", "ok")
    except Exception as e:
        flash(f"Error enviando email: {e}", "error")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/<int:user_id>/tracker")
@login_required
def admin_ver_tracker(user_id):
    """Ve el tracker personal de un miembro (solo admin)."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    db = get_db()
    target = db.execute("SELECT id, nombre, email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin_miembros"))
    rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC LIMIT 100",
        (user_id,),
    ).fetchall()
    tracked = [dict(r) for r in rows]
    stats = compute_stats(tracked) if tracked else {}
    return render_template("admin_miembro_tracker.html", miembro=dict(target),
                           tracked=tracked, stats=stats)


# ============ Liga por jugada del archivo (compartido F2/F3) ==============
# El campo `liga` del archivo solo existe en las jugadas recientes; para el
# resto se deriva DETERMINÍSTICAMENTE del pick con este mapa equipo→liga
# (verificado contra las etiquetas del registro: la suma por liga cuadra
# exacta con el récord oficial). Si un equipo futuro no está en el mapa, la
# función devuelve None y las superficies que dependen de la liga se
# ocultan antes que mostrar un dato dudoso.
_TEAM_LEAGUE = [
    ("Buffalo Bills", "NFL"), ("Los Angeles Rams", "NFL"),
    ("Green Bay Packers", "NFL"), ("Chicago Bears", "NFL"),
    ("Indianapolis Colts", "NFL"), ("Pittsburgh Steelers", "NFL"),
    ("New Orleans Saints", "NFL"), ("Atlanta Falcons", "NFL"),
    ("Dallas Cowboys", "NFL"), ("Houston Texans", "NFL"),
    ("San Francisco 49ers", "NFL"), ("Denver Broncos", "NFL"),
    ("Cleveland Browns", "NFL"), ("Carolina Panthers", "NFL"),
    ("Arizona Cardinals", "NFL"), ("Los Angeles Chargers", "NFL"),
    ("Las Vegas Raiders", "NFL"), ("Philadelphia Eagles", "NFL"),
    ("Bills", "NFL"), ("Rams", "NFL"), ("Packers", "NFL"), ("Bears", "NFL"),
    ("Colts", "NFL"), ("Steelers", "NFL"), ("Saints", "NFL"),
    ("Falcons", "NFL"), ("Cowboys", "NFL"), ("Texans", "NFL"),
    ("49ers", "NFL"), ("Broncos", "NFL"), ("Browns", "NFL"),
    ("Panthers", "NFL"), ("Cardinals", "NFL"), ("Chargers", "NFL"),
    ("Raiders", "NFL"), ("Eagles", "NFL"),
    ("Houston Cougars", "NCAAF"), ("Kansas State Wildcats", "NCAAF"),
    ("Kansas State", "NCAAF"), ("Penn State", "NCAAF"),
    ("Northwestern", "NCAAF"), ("Cougars", "NCAAF"), ("Wildcats", "NCAAF"),
    ("Milwaukee Brewers", "MLB"), ("Los Angeles Dodgers", "MLB"),
    ("Cleveland Guardians", "MLB"), ("Chicago White Sox", "MLB"),
    ("Atlanta Braves", "MLB"), ("Tampa Bay Rays", "MLB"),
    ("New York Yankees", "MLB"), ("San Diego Padres", "MLB"),
    ("Chicago Cubs", "MLB"), ("Houston Astros", "MLB"),
    ("Philadelphia Phillies", "MLB"), ("Washington Nationals", "MLB"),
    ("New York Mets", "MLB"), ("Arizona Diamondbacks", "MLB"),
    ("Miami Marlins", "MLB"), ("Boston Red Sox", "MLB"),
    ("Brewers", "MLB"), ("Dodgers", "MLB"), ("Guardians", "MLB"),
    ("White Sox", "MLB"), ("Braves", "MLB"), ("Rays", "MLB"),
    ("Yankees", "MLB"), ("Padres", "MLB"), ("Cubs", "MLB"),
    ("Astros", "MLB"), ("Phillies", "MLB"), ("Nationals", "MLB"),
    ("Mets", "MLB"), ("Diamondbacks", "MLB"), ("Marlins", "MLB"),
    ("Red Sox", "MLB"),
    ("Carolina Hurricanes", "NHL"), ("Washington Capitals", "NHL"),
    ("San Jose Sharks", "NHL"), ("Edmonton Oilers", "NHL"),
    ("Calgary Flames", "NHL"), ("Anaheim Ducks", "NHL"),
    ("Buffalo Sabres", "NHL"), ("Dallas Stars", "NHL"),
    ("New York Rangers", "NHL"),
    ("Hurricanes", "NHL"), ("Capitals", "NHL"), ("Sharks", "NHL"),
    ("Oilers", "NHL"), ("Flames", "NHL"), ("Ducks", "NHL"),
    ("Sabres", "NHL"), ("Stars", "NHL"), ("Rangers", "NHL"),
]


def _league_of_archive_play(j):
    lg = (j.get("liga") or "").upper()
    if lg in ("NFL", "NCAAF", "MLB", "NHL", "NBA"):
        return lg
    pick = j.get("pick") or ""
    for name, liga in _TEAM_LEAGUE:
        if re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])",
                     pick):
            return liga
    return None


# ================= Share de victorias (2026-10-14, pedido por Alex) =======
# Imagen vertical 1080×1920 de cada jugada oficial GANADA, generada en el
# servidor con PIL desde los datos REALES del archivo (pick, cuota,
# marcador final, profit y la foto del ticket original). Si a la jugada
# le falta ticket, marcador parseable o liga, el botón Share simplemente
# no aparece — jamás se fabrica un número ni un marcador.
_MARCADOR_RE = re.compile(
    r"^\s*(.+?)\s+(\d+)\s*[-–]\s*(\d+)\s+(?:vs|@)\s+(.+?)\s*$")


def _archive_play_id(fecha, jugada, seen):
    pid = jugada.get("id")
    if pid:
        return pid
    slug = re.sub(r"[^a-z0-9]+", "-",
                  (jugada.get("pick") or "play").lower()).strip("-")[:48]
    base = f"{fecha}-{slug}"
    pid, n = base, 2
    while pid in seen:
        pid = f"{base}-{n}"
        n += 1
    return pid


def _archive_index():
    """{play_id: (fecha, jugada)} de TODO el archivo oficial."""
    idx = {}
    for d in load_archive():
        fecha = d.get("fecha") or ""
        for j in d.get("jugadas", []) or []:
            idx[_archive_play_id(fecha, j, idx)] = (fecha, j)
    return idx


def _pick_short(pick):
    p = (pick or "").strip()
    for sep in (" vs ", " @ "):
        if sep in p:
            return p.split(sep)[0].strip()
    return p


def _marcador_labels(j, name_a, name_b):
    """Etiquetas cortas del marcador: las abreviaturas del archivo si se
    identifica nuestro lado; si no, la última palabra de cada nombre TAL
    como viene escrito en el marcador (los números nunca se alteran)."""
    def lastw(n):
        n = (n or "").strip()
        return n.split()[-1] if n else ""
    abbr = (j.get("abbr") or "").upper()
    oabbr = (j.get("opp_abbr") or "").upper()
    if abbr and oabbr:
        seg = re.sub(r"\s+(ML|[+-]\d+(?:\.\d+)?)\s*$", "",
                     _pick_short(j.get("pick") or ""))
        ours_last = lastw(seg).lower()
        if ours_last and ours_last == lastw(name_a).lower():
            return abbr, oabbr
        if ours_last and ours_last == lastw(name_b).lower():
            return oabbr, abbr
    return lastw(name_a), lastw(name_b)


def _share_data(fecha, j):
    """Datos para la imagen Share de UNA jugada, o None si no es
    compartible (no ganada, sin ticket, sin marcador parseable o sin
    liga derivable). Todo sale del archivo oficial — nada inventado."""
    if j.get("resultado") != "WON" or j.get("bloqueada"):
        return None
    comp = j.get("comprobante")
    if not comp:
        return None
    if not os.path.exists(os.path.join(BASE_DIR, "static", comp)):
        return None
    m = _MARCADOR_RE.match(j.get("marcador") or "")
    if not m:
        return None
    liga = _league_of_archive_play(j)
    if not liga:
        return None
    la, lb = _marcador_labels(j, m.group(1), m.group(4))
    if not la or not lb:
        return None
    profit = float(j.get("profit") or 0.0)
    nv = (j.get("nivel") or "").upper()
    return {
        "fecha": fecha,
        "pick": j.get("pick") or "",
        "pick_short": _pick_short(j.get("pick") or ""),
        "odds": fmt_odds(j.get("cuota")),
        "nivel_label": "PLATINUM" if nv in ("ELITE", "PLATINUM")
                       else (nv or "GOLD"),
        "liga": liga,
        "la": la, "sa": int(m.group(2)), "sb": int(m.group(3)), "lb": lb,
        "profit": profit,
        "units": profit / 25.0,  # normalización oficial: 1u = $25
        "comprobante": comp,
    }


def _share_info(play_id):
    got = _archive_index().get(play_id)
    if not got:
        return None
    return _share_data(got[0], got[1])


def _share_font(bold, size):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for cand in (os.path.join(BASE_DIR, "static", "fonts", name),
                 "/usr/share/fonts/truetype/dejavu/" + name):
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            pass
    return ImageFont.load_default(size=size)


def _draw_tracked(d, cx, y, text, font, fill, tracking=0):
    widths = [d.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x = cx - total / 2.0
    for ch, w in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill)
        x += w + tracking
    return total


def _fit_font(d, text, bold, max_w, start):
    size = start
    while size > 18:
        f = _share_font(bold, size)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _share_font(bold, size)


def _generate_share_image(play_id):
    """Genera (y cachea en static/img/share/) la imagen Share de la
    jugada. Devuelve la ruta del PNG o None si no es compartible."""
    info = _share_info(play_id)
    if not info:
        return None
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1080, 1920
    GOLD = (232, 200, 90)
    GOLD_D = (201, 162, 39)
    MINT = (61, 220, 151)
    WHITE = (245, 247, 255)
    GRAY = (154, 166, 196)
    # Fondo: degradado vertical navy + glow dorado suave tras el héroe
    img = Image.new("RGB", (W, H))
    d0 = ImageDraw.Draw(img)
    top, bot = (20, 27, 49), (5, 8, 26)
    for y in range(H):
        t = y / (H - 1)
        d0.line([(0, y), (W, y)], fill=tuple(
            int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([70, -180, 1010, 760], fill=(212, 160, 39, 55))
    img = Image.alpha_composite(
        img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(130)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([24, 24, W - 24, H - 24], radius=36,
                        outline=GOLD_D, width=3)
    # Logo circular con aro dorado
    try:
        logo = Image.open(os.path.join(
            BASE_DIR, "static", "img", "sharp-team-logo.png")).convert("RGBA")
        side = min(logo.size)
        logo = logo.crop(((logo.width - side) // 2, (logo.height - side) // 2,
                          (logo.width + side) // 2, (logo.height + side) // 2)
                         ).resize((210, 210))
        mask = Image.new("L", (210, 210), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, 210, 210], fill=255)
        img.paste(logo, (W // 2 - 105, 110), mask)
        d.ellipse([W // 2 - 107, 108, W // 2 + 107, 322],
                  outline=GOLD, width=4)
    except Exception:
        pass
    _draw_tracked(d, W // 2, 348, "THE SHARP TEAM",
                  _share_font(True, 58), GOLD, tracking=6)
    d.line([(370, 428), (710, 428)], fill=GOLD_D, width=3)
    # WINNER con badge de check menta
    wf = _share_font(True, 118)
    wt = d.textlength("WINNER", font=wf)
    badge_r, gap = 52, 30
    gx = (W - (badge_r * 2 + gap + wt)) / 2
    cy = 480 + 62
    cxb = gx + badge_r
    d.ellipse([cxb - badge_r, cy - badge_r, cxb + badge_r, cy + badge_r],
              fill=MINT)
    d.line([(cxb - 24, cy + 0), (cxb - 7, cy + 18), (cxb + 27, cy - 21)],
           fill=(6, 20, 14), width=11, joint="curve")
    d.text((gx + badge_r * 2 + gap, 480), "WINNER", font=wf, fill=WHITE)
    # Píldora de nivel · liga
    pill_txt = f"{info['nivel_label']} PLAY · {info['liga']}"
    pf = _share_font(True, 38)
    tw = sum(d.textlength(c, font=pf) for c in pill_txt) + 3 * len(pill_txt)
    d.rounded_rectangle([W // 2 - tw / 2 - 36, 618, W // 2 + tw / 2 + 36, 682],
                        radius=32, fill=(42, 33, 19), outline=GOLD_D, width=3)
    _draw_tracked(d, W // 2, 630, pill_txt, pf, GOLD, tracking=3)
    # Pick + marcador + profit (todo del archivo oficial)
    pick_line = f"{info['pick_short']} ({info['odds']})"
    d.text((W / 2, 712), pick_line,
           font=_fit_font(d, pick_line, True, 950, 60), fill=WHITE,
           anchor="ma")
    score_line = (f"{info['la']} {info['sa']} – {info['sb']} {info['lb']}"
                  " · Final")
    d.text((W / 2, 792), score_line,
           font=_fit_font(d, score_line, True, 950, 64), fill=GOLD,
           anchor="ma")
    d.text((W / 2, 878), f"Profit: +${info['profit']:,.2f}",
           font=_share_font(True, 92), fill=MINT, anchor="ma")
    d.text((W / 2, 990), f"{info['units']:+.2f} units",
           font=_share_font(True, 46), fill=MINT, anchor="ma")
    d.line([(90, 1062), (W - 90, 1062)], fill=(70, 82, 120), width=2)
    # Ticket original (foto real del archivo) con borde dorado
    ty = 1092
    th = 0
    try:
        tk = Image.open(os.path.join(
            BASE_DIR, "static", info["comprobante"])).convert("RGB")
        tk = tk.resize((880, int(tk.height * 880 / tk.width)))
        if tk.height > 548:
            topc = int(tk.height * 0.12)
            tk = tk.crop((0, topc, 880, topc + 548))
        th = tk.height
        tmask = Image.new("L", tk.size, 0)
        ImageDraw.Draw(tmask).rounded_rectangle(
            [0, 0, tk.width, tk.height], radius=26, fill=255)
        img.paste(tk, (100, ty), tmask)
        d.rounded_rectangle([100, ty, 980, ty + th], radius=26,
                            outline=GOLD_D, width=3)
    except Exception:
        th = 0
    if th:
        _draw_tracked(d, W // 2, ty + th + 22, "ORIGINAL TICKET · NOVIG",
                      _share_font(True, 33), (139, 150, 181), tracking=4)
    d.text((W / 2, 1748), "Every play posted before game time — locked & graded.",
           font=_fit_font(d, "Every play posted before game time — locked & graded.",
                          False, 950, 39),
           fill=(199, 206, 222), anchor="ma")
    d.text((W / 2, 1808), "thesharpteam.bet",
           font=_share_font(True, 54), fill=GOLD, anchor="ma")
    out_dir = os.path.join(BASE_DIR, "static", "img", "share")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{play_id}.png")
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def _share_image_path(play_id):
    if not re.fullmatch(r"[A-Za-z0-9\-]+", play_id or ""):
        return None
    out_path = os.path.join(BASE_DIR, "static", "img", "share",
                            f"{play_id}.png")
    if os.path.exists(out_path):
        return out_path
    return _generate_share_image(play_id)


@app.route("/share/<play_id>")
@login_required
def share_play(play_id):
    """Página Share de una victoria oficial (2026-10-14): muestra la
    imagen generada desde el archivo y el botón de descarga."""
    info = _share_info(play_id)
    if not info:
        from flask import abort
        abort(404)
    return render_template("share_play.html", info=info, play_id=play_id)


@app.route("/share/<play_id>/image.png")
@login_required
def share_play_image(play_id):
    from flask import abort, send_file
    path = _share_image_path(play_id)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/official-tracker")
@login_required
def official_tracker():
    """Tracker EJECUTIVO del programa para miembros (2026-10-12, pedido por
    Alex): el sofisticado que será el oficial del programa — mismas stats
    completas que la versión del Admin. El tracker clásico de miembros
    (/resultados) queda igual. El link bajo la tarjeta de números del home
    apunta aquí."""
    t = official_tracker_full()
    # Contabilidad por programa (2026-10-15, pedido por Alex): cada
    # tarjeta del home (Official / Platinum / Gold) abre el libro de SU
    # programa: sus jugadas, cada una con su informacion y su ticket.
    programa = (request.args.get("programa") or "all").strip().lower()
    if programa not in ("all", "platinum", "gold"):
        programa = "all"
    if programa == "platinum":
        libro = [x for x in t["historial"] if x["nivel"] in ("ELITE", "PLATINUM")]
        libro_stats, libro_titulo = t["por_nivel"]["ELITE"], "Platinum plays"
    elif programa == "gold":
        libro = [x for x in t["historial"] if x["nivel"] == "GOLD"]
        libro_stats, libro_titulo = t["por_nivel"]["GOLD"], "Gold plays"
    else:
        libro, libro_stats, libro_titulo = t["historial"], t["general"], "All official plays"
    return render_template("official_tracker.html", t=t, programa=programa,
                           libro=libro, libro_stats=libro_stats,
                           libro_titulo=libro_titulo)


@app.route("/admin/tracker-oficial")
@login_required
def admin_tracker_oficial():
    """Tracker oficial ESPECIAL del Admin (2026-10-12, pedido por Alex):
    más estadísticas y más profesional que el del Dashboard de miembro.
    Solo admin o la cuenta de miembro de Alex, igual que /admin."""
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    return render_template("admin_tracker_oficial.html", t=official_tracker_full())


@app.route("/admin/miembros/email", methods=["POST"])
@login_required
def admin_cambiar_email():
    """Cambia el email de una cuenta de miembro (solo admin). Valida unicidad."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
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


@app.route("/admin/migrar-track", methods=["GET", "POST"])
@login_required
def admin_migrar_track():
    """Migra el historial de trackeo (jugadas + checkins + bankroll) de una
    cuenta a otra. Solo admin. No toca is_admin, platinum_unlocked ni datos de Stripe."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
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
    if not _admin_o_cuenta_alex(user):
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

@app.route("/admin/resend-welcome", methods=["GET", "POST"])
def admin_resend_welcome():
    """Reenvía el email de bienvenida a un miembro.
    Auth: sesión admin O header X-Admin-Key.
    Resetea welcome_email_sent y lo envía de inmediato."""
    if not _admin_api_autorizado():
        return "admin only", 403
    from flask import request
    db = get_db()
    email = (request.values.get("email") or "").strip().lower()
    if not email:
        return "Falta ?email=", 400
    row = db.execute("SELECT id, nombre FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return f"No existe cuenta con {email}", 404
    try:
        send_welcome_email(row["nombre"] or "", email)
        db.execute("UPDATE users SET welcome_email_sent = 1 WHERE id = ?", (row["id"],))
        db.commit()
        return f"Email de bienvenida reenviado a {email}", 200
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/admin/send-monthly", methods=["GET", "POST"])
@admin_required
def admin_send_monthly():
    """Envía la liquidación mensual a un miembro (solo admin).

    Uso: /admin/send-monthly?email=X&month=YYYY-MM (month default: mes anterior).
    Usa los números REALES del tracker personal del miembro.
    """
    from flask import request
    import re as _re
    email = (request.values.get("email") or "").strip().lower()
    month = (request.values.get("month") or "").strip()
    if not email:
        return "Falta ?email=", 400
    if not month:
        _hoy = datetime.now(TZ)
        _pm = _hoy.month - 1 or 12
        _py = _hoy.year - (1 if _hoy.month == 1 else 0)
        month = f"{_py}-{_pm:02d}"
    if not _re.fullmatch(r"\d{4}-\d{2}", month):
        return "month debe ser YYYY-MM", 400
    ok, detail = send_monthly_statement(email, month)
    code = 200 if ok else 404
    return f"Liquidación {month} a {email}: {'enviada' if ok else 'NO enviada'} ({detail})", code

@app.route("/admin/migrate-alex-email", methods=["GET", "POST"])
@admin_required
def admin_migrate_alex_email():
    """TEMPORAL (2026-10-06, pedido por Alex): migra el email de su cuenta
    de miembro de alexpena1509@gmail.com a pena.manuel@myyahoo.com.
    Idempotente: si ya está migrado, lo reporta sin tocar nada."""
    from flask import request
    db = get_db()
    old = "alexpena1509@gmail.com"
    new = ALEX_MEMBER_EMAIL
    ya = db.execute("SELECT id FROM users WHERE LOWER(email) = ?",
                    (new.lower(),)).fetchone()
    if ya:
        msg = f"Ya migrado: la cuenta {new} existe (id {ya['id']})"
    else:
        row = db.execute("SELECT id, nombre FROM users WHERE LOWER(email) = ?",
                         (old,)).fetchone()
        if not row:
            msg = f"No existe cuenta con {old}"
        else:
            db.execute("UPDATE users SET email = ? WHERE id = ?", (new, row["id"]))
            db.commit()
            msg = f"Email migrado: {old} -> {new} (id {row['id']})"
    return (f"<!doctype html><html><head><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Migración</title></head>"
            f"<body style='background:#0b0e14;color:#fff;font-family:sans-serif;"
            f"padding:40px 20px;text-align:center;'>"
            f"<h2 style='color:#c9a227;'>Migración de email</h2>"
            f"<p style='font-size:1.1rem;'>{msg}</p>"
            f"<p><a href='/admin' style='color:#c9a227;'>← Volver al panel</a></p>"
            f"</body></html>"), 200

@app.route("/admin/diag-alex-track")
@admin_required
def diag_alex_track():
    """TEMPORAL: desglose del tracker de Alex por nivel."""
    from flask import Response
    db = get_db()
    alex = db.execute(
        "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL.lower(),)
    ).fetchone()
    if not alex:
        return Response("Alex no encontrado", mimetype="text/plain")
    uid = alex["id"]
    rows = db.execute(
        "SELECT id, play_id, fecha, nivel, pick, cuota, stake_monto, "
        "resultado, comprobante, created_at "
        "FROM tracked_plays WHERE user_id = ? ORDER BY id",
        (uid,),
    ).fetchall()
    data = [dict(r) for r in rows]
    # Resumen por nivel
    summary = {}
    for r in data:
        niv = r.get("nivel") or "NULL"
        summary[niv] = summary.get(niv, 0) + 1
    out = {"total": len(data), "por_nivel": summary, "plays": data}
    return Response(json.dumps(out, indent=1, ensure_ascii=False),
                    mimetype="application/json")

@app.route("/admin/fix-alex-dups")
@admin_required
def fix_alex_dups():
    """TEMPORAL (2026-10-04): elimina duplicados del tracker de Alex.
    Mantiene el registro oficial de 20 jugadas (15-5, +$166.64)."""
    from flask import Response
    db = get_db()
    alex = db.execute(
        "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL.lower(),)
    ).fetchone()
    if not alex:
        return Response("Alex no encontrado", mimetype="text/plain")
    uid = alex["id"]
    # Buscar duplicados por (pick, fecha, cuota)
    rows = db.execute(
        "SELECT id, play_id, pick, fecha, cuota FROM tracked_plays "
        "WHERE user_id = ? ORDER BY id",
        (uid,),
    ).fetchall()
    seen = {}
    to_delete = []
    for r in rows:
        key = (r["pick"], r["fecha"], r["cuota"])
        if key in seen:
            # Duplicado: borrar el de ID mayor (el más reciente)
            to_delete.append(r["id"])
        else:
            seen[key] = r["id"]
    for did in to_delete:
        db.execute("DELETE FROM tracked_plays WHERE id = ?", (did,))
    db.commit()
    return Response(
        f"Eliminados {len(to_delete)} duplicados: {to_delete}",
        mimetype="text/plain")

@app.route("/admin/seed-tracker")
@login_required
def admin_seed_tracker():
    """Importa una sola vez las jugadas liquidadas del programa al tracker personal del admin."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
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
                        _edge_num(j.get("edge")), "W" if j.get("resultado") == "WON" else "L",
                        now_iso(),
                    ),
                )
                added += 1
            except INTEGRITY_ERRORS:
                pass
    db.commit()
    flash(f"Tracker seeded: {added} settled plays imported.", "ok")
    return redirect(url_for("tracker"))


def log_notificacion(db, tipo, titulo, detalle=""):
    """Registra una notificación en el centro del admin (2026-10-07)."""
    try:
        from datetime import datetime, timezone
        ahora = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO notificaciones (tipo, titulo, detalle, created_at) VALUES (?, ?, ?, ?)",
            (tipo, titulo, detalle, ahora),
        )
        db.commit()
    except Exception:
        pass


def _value_stats_for_admin(db):
    """💎 VALUE tracker del sistema para el admin (2026-10-10, pedido por Alex).

    Alex opera desde el admin y no podía ver las VALUE plays (solo salían en la
    cuenta de miembro). Devuelve récord + historial del tracker VALUE simulado
    (user_id=0, VALUE System), igual que /api/values-record.
    """
    stats = {"wins": 0, "losses": 0, "profit": 0.0, "profit_display": "$0.00",
             "record": "0-0", "positive": True, "plays": []}
    try:
        ensure_value_system_user(db)
        _vrows = db.execute(
            "SELECT pick, cuota, fecha, resultado, stake_monto FROM tracked_plays "
            "WHERE user_id = ? AND nivel = 'VALUE' ORDER BY fecha DESC, id DESC LIMIT 30",
            (VALUE_SYSTEM_USER_ID,),
        ).fetchall()
        _vw = _vl = 0
        _vp = 0.0
        _vplays = []
        for _r in _vrows:
            _d = dict(_r)
            _p = play_profit_dollars({"cuota": _d["cuota"], "stake_monto": _d["stake_monto"] or 50.0,
                                      "resultado": _d["resultado"]})
            if _d["resultado"] == "W":
                _vw += 1
                _vp += _p
            elif _d["resultado"] == "L":
                _vl += 1
                _vp += _p
            _vplays.append({
                "pick": _d["pick"], "cuota": _d["cuota"], "fecha": _d["fecha"],
                "resultado": _d["resultado"] or "PENDING",
                "profit": round(_p, 2),
            })
        stats = {
            "wins": _vw, "losses": _vl,
            "profit": round(_vp, 2), "profit_display": fmt_big_dollars(_vp),
            "record": f"{_vw}-{_vl}", "positive": _vp >= 0,
            "plays": _vplays,
        }
    except Exception:
        pass
    return stats


@app.route("/admin")
@login_required
def admin_dashboard():
    """Panel central del administrador — SOLO cuentas con is_admin=1
    (2026-10-15, orden de Alex: su cuenta de miembro es un miembro
    normal, sin acceso admin; el admin vive solo en su cuenta admin)."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    try:
        miembros = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    except Exception:
        miembros = 0
    try:
        platinum = db.execute("SELECT COUNT(*) c FROM users WHERE platinum_unlocked=1").fetchone()["c"]
    except Exception:
        platinum = 0
    try:
        _, plays = load_data()
        jugadas_hoy = len(plays) if plays else 0
    except Exception:
        jugadas_hoy = 0
        plays = []
    try:
        no_leidas = db.execute("SELECT COUNT(*) c FROM notificaciones WHERE leida = 0").fetchone()["c"]
    except Exception:
        no_leidas = 0
    # Selecciones pendientes (antes de publicarse) — 2026-10-07, pedido por Alex
    # Solo las de HOY — las de ayer se limpian a medianoche (2026-10-09, pedido por Alex)
    import json as _json2, os as _os2
    from datetime import datetime as _dt2
    _hoy = _dt2.now().strftime("%Y-%m-%d")
    selecciones = []
    try:
        _data = _json_remoto_o_local(PENDING_RAW_URL, PENDING_PLAYS_PATH)
        if isinstance(_data, dict) and _data.get("fecha") == _hoy:
            selecciones = _data.get("plays", [])
    except Exception:
        selecciones = []
    # plays_hoy también solo de hoy — la fecha está en la raíz del JSON, no en cada jugada
    try:
        _d = _json_remoto_o_local(PLAYS_RAW_URL, PLAYS_PATH)
        _root_fecha = str(_d.get("fecha", ""))[:10] if isinstance(_d, dict) else ""
        _, _plays_all = load_data()
        if _root_fecha == _hoy:
            plays = _plays_all or []
        else:
            plays = []
        jugadas_hoy = len(plays)
    except Exception:
        jugadas_hoy = 0
        plays = []
    return render_template("admin_dashboard.html",
                           stats={"miembros": miembros, "platinum": platinum,
                                  "jugadas_hoy": jugadas_hoy, "no_leidas": no_leidas},
                           selecciones=selecciones,
                           plays_hoy=plays,
                           oficial=program_stats(),
                           tracked_ids=[])


# ============================================================
#  CONTROL MANUAL DEL ADMIN (2026-10-17, pedido de Alex:
#  "métele potencia, más manera de controlar manualmente")
# ============================================================

@app.route("/admin/control/push", methods=["POST"])
@admin_required
def admin_control_push():
    """Push manual a todos los miembros suscritos (respeta notif_plays)."""
    titulo = (request.form.get("titulo") or "").strip()[:80]
    mensaje = (request.form.get("mensaje") or "").strip()[:240]
    if not titulo or not mensaje:
        flash("Push: falta el título o el mensaje.", "error")
        return redirect(url_for("admin_dashboard"))
    if not HAVE_WEBPUSH or not VAPID_PRIVATE_KEY:
        flash("El push no está configurado en el servidor.", "error")
        return redirect(url_for("admin_dashboard"))
    payload = json.dumps({"title": titulo, "body": mensaje, "url": "/home", "image": ""})
    db = get_db()
    subs = db.execute(
        "SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps"
        " JOIN users u ON u.id = ps.member_id WHERE u.notif_plays = 1"
    ).fetchall()
    sent = failed = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"],
                                   "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
            )
            sent += 1
        except WebPushException as ex:
            code = getattr(getattr(ex, "response", None), "status_code", None)
            if code in (404, 410):
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
            failed += 1
        except Exception:
            failed += 1
    db.commit()
    flash(f"🔔 Push enviado: {sent} entregados, {failed} fallidos.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/control/email", methods=["POST"])
@admin_required
def admin_control_email():
    """Email de anuncio manual a todos los miembros reales."""
    asunto = (request.form.get("asunto") or "").strip()[:120]
    mensaje = (request.form.get("mensaje") or "").strip()[:4000]
    if not asunto or not mensaje:
        flash("Email: falta el asunto o el mensaje.", "error")
        return redirect(url_for("admin_dashboard"))
    import html as _html
    parrafos = "".join(
        f'<p style="margin:0 0 10px;font-size:13.5px;line-height:1.65;color:#dbe4f5;">{_html.escape(p)}</p>'
        for p in mensaje.split("\n") if p.strip()
    )
    html_body = (
        '<div style="background:#070d1d;padding:18px 12px;">'
        '<div style="font-family:Arial,sans-serif;max-width:440px;margin:0 auto;background:#0e1730;border-radius:14px;padding:22px 24px;">'
        '<p style="margin:0 0 4px;font-size:11px;font-weight:800;letter-spacing:3px;color:#e8c766;">THE SHARP TEAM</p>'
        f'<h1 style="font-family:Georgia,serif;font-size:19px;color:#ffffff;margin:6px 0 12px;">{_html.escape(asunto)}</h1>'
        + parrafos +
        '<a href="https://www.thesharpteam.bet/home" style="display:inline-block;background:#2f7bff;color:#ffffff;font-size:12.5px;font-weight:800;padding:11px 22px;border-radius:999px;text-decoration:none;margin-top:8px;">OPEN MY DASHBOARD</a>'
        '<p style="margin:16px 0 0;color:#7e8db0;font-size:9.5px;">21+. Play responsibly. If you or someone you know has a gambling problem, call or text 1-800-GAMBLER.</p>'
        '</div></div>'
    )
    db = get_db()
    filas = db.execute("SELECT email FROM users WHERE is_admin = 0").fetchall()
    sent = 0
    for f in filas:
        email = (f["email"] or "").strip()
        if not email or "@value.local" in email:
            continue
        ok, _det = _dispatch_email(email, asunto, mensaje, html_body)
        if ok:
            sent += 1
    flash(f"📧 Email enviado a {sent} miembros.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/control/grant", methods=["POST"])
@admin_required
def admin_control_grant():
    """Dar Platinum a un miembro por email (días vacío = permanente)."""
    email = (request.form.get("email") or "").strip().lower()
    dias_raw = (request.form.get("dias") or "").strip()
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
    if not u:
        flash(f"No existe un miembro con el email {email}.", "error")
        return redirect(url_for("admin_dashboard"))
    if dias_raw:
        try:
            dias = max(1, int(dias_raw))
        except ValueError:
            dias = 30
        from datetime import datetime as _dt3, timedelta as _td3, timezone as _tz3
        fin = (_dt3.now(_tz3.utc) + _td3(days=dias)).isoformat()
        db.execute("UPDATE users SET platinum_unlocked = 1, platinum_trial_ends = ? WHERE id = ?",
                   (fin, u["id"]))
        detalle = f"Platinum activo hasta el {fin[:10]}"
    else:
        db.execute("UPDATE users SET platinum_unlocked = 1, platinum_trial_ends = NULL WHERE id = ?",
                   (u["id"],))
        detalle = "Platinum permanente (sin reloj)"
    db.commit()
    flash(f"🎁 {email}: {detalle}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/control/trial-all", methods=["POST"])
@admin_required
def admin_control_trial_all():
    """Mes gratis de Platinum para todos los miembros sin Stripe.
    No toca: cuentas que ya pagan, ni regalos permanentes (sin reloj)."""
    from datetime import datetime as _dt4, timedelta as _td4, timezone as _tz4
    fin = (_dt4.now(_tz4.utc) + _td4(days=30)).isoformat()
    db = get_db()
    cur = db.execute(
        "UPDATE users SET platinum_unlocked = 1, platinum_trial_ends = ?"
        " WHERE is_admin = 0 AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')"
        " AND email NOT LIKE ?"
        " AND (platinum_unlocked = 0 OR platinum_trial_ends IS NOT NULL)",
        (fin, "%@value.local"),
    )
    db.commit()
    n = cur.rowcount if cur.rowcount is not None else 0
    flash(f"⚡ Mes gratis activado/renovado para {n} miembros (vence {fin[:10]}).", "success")
    return redirect(url_for("admin_dashboard"))



# ============================================================
#  ACTIVIDAD DE MIEMBROS (2026-10-17, pedido de Alex): quien
#  entra al Dashboard, cuantas veces al dia y desde que ciudad.
# ============================================================
_ultima_visita = {}


def _ip_cliente():
    try:
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xff or (request.remote_addr or "")
    except Exception:
        return ""


def _registrar_actividad(user_id, kind):
    try:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS member_activity (user_id INTEGER, kind TEXT, ip TEXT, created_at TEXT)")
        from datetime import datetime as _dta, timezone as _tza
        db.execute(
            "INSERT INTO member_activity (user_id, kind, ip, created_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, _ip_cliente(), _dta.now(_tza.utc).isoformat()),
        )
        db.commit()
    except Exception:
        pass


def _registrar_visita(user):
    """Visita al Dashboard con freno de 30 min por miembro (cuenta sesiones)."""
    try:
        if not user:
            return
        import time as _tm
        uid = user["id"]
        ahora = _tm.time()
        if ahora - _ultima_visita.get(uid, 0) < 1800:
            return
        _ultima_visita[uid] = ahora
        _registrar_actividad(uid, "visit")
    except Exception:
        pass


def _ciudad_de_ip(db, ip):
    if not ip:
        return ""
    g = db.execute("SELECT city, region, country FROM geo_cache WHERE ip = ?", (ip,)).fetchone()
    if g:
        return ", ".join([x for x in [g["city"], g["region"]] if x]) or (g["country"] or "")
    city = region = country = ""
    if not ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.", "127.", "::1")):
        try:
            import urllib.request as _ur, json as _js
            req = _ur.Request(
                f"http://ip-api.com/json/{ip}?fields=status,city,regionName,country",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with _ur.urlopen(req, timeout=3) as r:
                d = _js.loads(r.read().decode("utf-8", "ignore"))
            if d.get("status") == "success":
                city, region, country = d.get("city") or "", d.get("regionName") or "", d.get("country") or ""
        except Exception:
            pass
    try:
        db.execute(
            "INSERT INTO geo_cache (ip, city, region, country) VALUES (?, ?, ?, ?) ON CONFLICT(ip) DO NOTHING",
            (ip, city, region, country),
        )
        db.commit()
    except Exception:
        pass
    return ", ".join([x for x in [city, region] if x]) or country


@app.route("/admin/actividad")
@admin_required
def admin_actividad():
    """Panel de actividad de miembros: entradas, sesiones y trackeos."""
    db = get_db()
    db.execute("CREATE TABLE IF NOT EXISTS member_activity (user_id INTEGER, kind TEXT, ip TEXT, created_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS geo_cache (ip TEXT PRIMARY KEY, city TEXT, region TEXT, country TEXT)")
    db.commit()
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc
    corte = datetime.now(et).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()

    def fmt(ts):
        if not ts:
            return "—"
        try:
            d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(et).strftime("%b %d, %I:%M %p")
        except Exception:
            return str(ts)[:16]

    miembros = db.execute(
        "SELECT id, nombre, email, platinum_unlocked, phone, lang, created_at,"
        " bankroll, stake_mode, stake_fijo_elite, stake_fijo_gold,"
        " stripe_subscription_id, stripe_customer_id, cancel_requested_at,"
        " telegram_user_id, platinum_trial_ends, sms_optin FROM users"
        " WHERE is_admin = 0 AND email NOT LIKE ? ORDER BY nombre",
        ("%@value.local",),
    ).fetchall()
    datos = []
    for m in miembros:
        uid = m["id"]
        ult = db.execute(
            "SELECT created_at, ip FROM member_activity WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (uid,),
        ).fetchone()
        entradas_hoy = db.execute(
            "SELECT COUNT(*) c FROM member_activity WHERE user_id = ? AND created_at >= ?",
            (uid, corte),
        ).fetchone()["c"]
        logins_hoy = db.execute(
            "SELECT COUNT(*) c FROM member_activity WHERE user_id = ? AND kind = 'login' AND created_at >= ?",
            (uid, corte),
        ).fetchone()["c"]
        tracks = db.execute(
            "SELECT pick, nivel, fecha, resultado FROM tracked_plays WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (uid,),
        ).fetchall()
        total_tracks = db.execute(
            "SELECT COUNT(*) c FROM tracked_plays WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        # --- Expediente completo del miembro (2026-10-17) ---
        tot_entradas = db.execute(
            "SELECT COUNT(*) c FROM member_activity WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        dias_activo = db.execute(
            "SELECT COUNT(*) c FROM checkins WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        dispositivos = db.execute(
            "SELECT COUNT(*) c FROM push_subscriptions WHERE member_id = ?", (uid,)
        ).fetchone()["c"]
        wl = db.execute(
            "SELECT resultado, COUNT(*) c FROM tracked_plays WHERE user_id = ?"
            " AND resultado IN ('W', 'L') GROUP BY resultado", (uid,)
        ).fetchall()
        wins = sum(r["c"] for r in wl if r["resultado"] == "W")
        losses = sum(r["c"] for r in wl if r["resultado"] == "L")
        liquidadas = db.execute(
            "SELECT cuota, stake_monto, resultado FROM tracked_plays"
            " WHERE user_id = ? AND resultado IN ('W', 'L')", (uid,)
        ).fetchall()
        profit = 0.0
        for t in liquidadas:
            monto = float(t["stake_monto"] or 0)
            if t["resultado"] == "W":
                q = t["cuota"] or 0
                profit += monto * (q / 100.0) if q > 0 else monto * (100.0 / abs(q)) if q else 0
            else:
                profit -= monto
        if m["stripe_subscription_id"]:
            estado = "Pagando por Stripe"
        elif m["platinum_trial_ends"]:
            estado = "Mes gratis hasta " + str(m["platinum_trial_ends"])[:10]
        elif m["platinum_unlocked"]:
            estado = "Platinum permanente"
        else:
            estado = "Gratis (Gold)"
        datos.append({
            "nombre": m["nombre"], "email": m["email"],
            "platinum": bool(m["platinum_unlocked"]),
            "ultima": fmt(ult["created_at"]) if ult else "Sin registro aún",
            "con_registro": bool(ult),
            "ciudad": _ciudad_de_ip(db, ult["ip"]) if ult else "",
            "entradas_hoy": entradas_hoy, "logins_hoy": logins_hoy,
            "tracks": [{"pick": t["pick"], "nivel": t["nivel"], "fecha": t["fecha"],
                        "resultado": t["resultado"]} for t in tracks],
            "total_tracks": total_tracks,
            "telefono": m["phone"] or "—",
            "idioma": "Español" if (m["lang"] or "en") == "es" else "English",
            "desde": fmt(m["created_at"]),
            "bankroll": ("$" + format(m["bankroll"], ",.2f")) if m["bankroll"] else "Sin configurar",
            "stake": ("Fijo P $" + str(m["stake_fijo_elite"]) + " / G $" + str(m["stake_fijo_gold"]))
                     if m["stake_fijo_elite"] else ("1% de su bankroll (" + (m["stake_mode"] or "units") + ")"),
            "estado": estado,
            "cancel_pedida": bool(m["cancel_requested_at"]),
            "telegram": "Vinculado" if m["telegram_user_id"] else "Sin vincular",
            "push": f"Activado ({dispositivos} disp.)" if dispositivos else "No activado",
            "sms": "Aceptó SMS" if m["sms_optin"] else "Sin SMS",
            "tot_entradas": tot_entradas, "dias_activo": dias_activo,
            "record": f"{wins}–{losses}",
            "profit": (("+$" if profit >= 0 else "−$") + format(abs(profit), ",.2f")),
            "profit_pos": profit >= 0,
        })
    datos.sort(key=lambda d: (not d["con_registro"], d["nombre"]))
    return render_template("admin_actividad.html", datos=datos)



def _tg_send_message(chat_id, text):
    """Envía texto por Telegram vía Bot API directo (funciona en Render,
    donde no existen los binarios de la VM). Devuelve (ok, detalle)."""
    if not TELEGRAM_BOT_TOKEN:
        return False, "falta TELEGRAM_BOT_TOKEN en Render"
    try:
        payload = json.dumps({"chat_id": str(chat_id), "text": text}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN,
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(500).decode("utf-8", "replace")
            return (200 <= r.status < 300), "telegram HTTP %s: %s" % (r.status, body[:150])
    except Exception as e:
        extra = ""
        try:
            extra = " [HTTP %s] %s" % (getattr(e, "code", "?"),
                                       e.read().decode("utf-8", "replace")[:200])
        except Exception:
            pass
        return False, "telegram falló: %s%s" % (e, extra)


def _tg_send_photo(chat_id, photo_path, caption=""):
    """Envía foto por Telegram vía Bot API directo (multipart, funciona en
    Render). Devuelve (ok, detalle)."""
    if not TELEGRAM_BOT_TOKEN:
        return False, "falta TELEGRAM_BOT_TOKEN en Render"
    try:
        import uuid as _uuid
        boundary = "----tst%s" % _uuid.uuid4().hex
        with open(photo_path, "rb") as f:
            photo_bytes = f.read()
        parts = []

        def _field(name, value):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, name, value)).encode("utf-8"))
        _field("chat_id", str(chat_id))
        _field("caption", caption)
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"photo\"; "
                      "filename=\"ticket.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
                      % boundary).encode("utf-8") + photo_bytes + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
        body = b"".join(parts)
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendPhoto" % TELEGRAM_BOT_TOKEN,
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = r.read(500).decode("utf-8", "replace")
            return (200 <= r.status < 300), "telegram HTTP %s: %s" % (r.status, resp[:150])
    except Exception as e:
        extra = ""
        try:
            extra = " [HTTP %s] %s" % (getattr(e, "code", "?"),
                                       e.read().decode("utf-8", "replace")[:200])
        except Exception:
            pass
        return False, "telegram foto falló: %s%s" % (e, extra)


def _push_aviso_disparo_alex(n_jugadas):
    """Aviso push INSTANTÁNEO solo para Alex cuando dispara jugadas en el
    Admin (2026-10-13, pedido por Alex): al darle "Enviar" le llega al
    teléfono la confirmación de recibido en el momento, sin esperar a que
    la publicación termine minutos después. Nunca lanza excepción: un
    fallo del push jamás bloquea el disparo."""
    try:
        if not (HAVE_WEBPUSH and VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY and webpush):
            return
        payload = json.dumps({
            "title": "🚀 Disparo recibido",
            "body": ("Recibí tu envío (%d jugada%s). Publicando en el Dashboard…"
                     % (n_jugadas, "s" if n_jugadas != 1 else "")),
            "url": "/admin",
            "image": "",
        })
        db = get_db()
        subs = db.execute(
            "SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps"
            " JOIN users u ON u.id = ps.member_id WHERE u.notif_plays = 1"
            " AND u.is_admin = 1"
        ).fetchall()
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
            except WebPushException as ex:
                code = getattr(getattr(ex, "response", None), "status_code", None)
                if code in (404, 410):
                    db.execute(
                        "DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
            except Exception:
                pass
        db.commit()
    except Exception:
        pass


@app.route("/admin/aprobar-jugadas", methods=["POST"])
@login_required
def admin_aprobar_jugadas():
    """BOTÓN DE DISPARO de publicación (2026-10-12, pedido por Alex).

    El botón NO publica directamente: registra las jugadas seleccionadas
    como una solicitud pendiente en la tabla publish_requests. El agente
    (cron vigilar-aprobaciones-admin) la recoge en minutos y hace la
    publicación REAL en el Dashboard de miembros: entradas completas en
    data/plays.json vía commit/push al repo, verificadas. Las jugadas se
    quedan en plays-pending.json hasta que la publicación se confirma.
    """
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    import json as _jsonB
    import os as _osB
    from datetime import datetime as _dtB
    _hoy = _dtB.now().strftime("%Y-%m-%d")
    _base = _osB.path.dirname(__file__)
    sel_ids = [str(s) for s in request.form.getlist("play_ids")]
    pendientes = []
    try:
        _d = _json_remoto_o_local(PENDING_RAW_URL, PENDING_PLAYS_PATH)
        if isinstance(_d, dict) and _d.get("fecha") == _hoy:
            pendientes = _d.get("plays", []) or []
    except Exception:
        pendientes = []
    elegidas = [p for p in pendientes if str(p.get("id")) in sel_ids]
    if not elegidas:
        flash("No plays selected.", "error")
        return redirect(url_for("admin_dashboard"))
    db = get_db()
    ya = db.execute(
        "SELECT play_ids FROM publish_requests WHERE fecha = ? AND status = 'pending'",
        (_hoy,)).fetchall()
    ya_ids = set()
    for _r in ya:
        try:
            ya_ids.update(_jsonB.loads(_r["play_ids"]))
        except Exception:
            pass
    nuevas = [str(p.get("id")) for p in elegidas if str(p.get("id")) not in ya_ids]
    if nuevas:
        db.execute(
            "INSERT INTO publish_requests (fecha, play_ids, status, created_at)"
            " VALUES (?, ?, 'pending', ?)",
            (_hoy, _jsonB.dumps(nuevas), _dtB.now(timezone.utc).isoformat()))
        db.commit()
        _push_aviso_disparo_alex(len(nuevas))
    return render_template("admin_aprobar_confirm.html",
                           elegidas=elegidas,
                           n_clientes=0,
                           modo_prueba=False,
                           hoy=_hoy,
                           tg_ok=[], tg_fail=[],
                           em_ok=[], em_fail=[])


@app.route("/admin/publish-pending", methods=["GET"])
def admin_publish_pending():
    """Solicitudes de publicación pendientes (para el cron del agente).
    Auth: X-Admin-Key o sesión admin."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    import json as _jsonC
    db = get_db()
    rows = db.execute(
        "SELECT id, fecha, play_ids, created_at FROM publish_requests"
        " WHERE status = 'pending' ORDER BY id").fetchall()
    out = []
    for r in rows:
        try:
            ids = _jsonC.loads(r["play_ids"])
        except Exception:
            ids = []
        out.append({"id": r["id"], "fecha": r["fecha"], "play_ids": ids,
                    "created_at": r["created_at"]})
    return jsonify({"pending": out})


@app.route("/admin/publish-done", methods=["POST"])
def admin_publish_done():
    """Marca una solicitud de publicación como completada.
    Auth: X-Admin-Key o sesión admin."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    from datetime import datetime as _dtC
    rid = (request.get_json(silent=True) or {}).get("id")
    if not rid:
        return jsonify({"error": "missing id"}), 400
    db = get_db()
    db.execute(
        "UPDATE publish_requests SET status = 'done', done_at = ? WHERE id = ?",
        (_dtC.now(timezone.utc).isoformat(), rid))
    db.commit()
    return jsonify({"ok": True, "id": rid})



@app.route("/admin/ticket-por-jugada", methods=["POST"])
@login_required
def admin_ticket_por_jugada():
    """Alex sube el ticket original de UNA jugada pendiente, directo desde su
    fila en el Admin (2026-10-10, pedido por Alex: "Hazlo más fácil").

    Guarda en static/img/comprobantes/<fecha>-<slug>.jpg y marca el campo
    "comprobante" en plays-pending.json, para que el flujo de aprobación lo
    encuentre y lo envíe. Devuelve JSON."""
    from flask import jsonify
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        return jsonify({"ok": False, "error": "sin permiso"}), 403
    import json as _jsonT, os as _osT, re as _reT
    from datetime import datetime as _dtT
    from werkzeug.utils import secure_filename
    _base = _osT.path.dirname(__file__)
    _play_id = (request.form.get("play_id") or "").strip()
    _f = request.files.get("ticket")
    if not _play_id or not _f or not _f.filename:
        return jsonify({"ok": False, "error": "Falta la jugada o la foto"}), 400

    # Cargar pendientes y localizar la jugada
    _pp_path = _osT.path.join(_base, "data", "plays-pending.json")
    _fecha = _dtT.now().strftime("%Y-%m-%d")
    try:
        with open(_pp_path, encoding="utf-8") as _fh:
            _d = _jsonT.load(_fh)
        if isinstance(_d, dict) and _d.get("fecha"):
            _fecha = str(_d.get("fecha"))[:10]
        _plays = _d.get("plays", []) or []
    except Exception:
        _plays = []
    _play = next((p for p in _plays if str(p.get("id")) == _play_id), None)
    if _play is None:
        return jsonify({"ok": False, "error": "Jugada no encontrada en pendientes"}), 404

    # Slug del nombre de archivo — igual que el flujo aprobar-jugadas
    _slug = _play_id
    _m = _reT.match(r"^play-(\d{4}-\d{2}-\d{2})-(.+)$", _play_id)
    if _m:
        _fecha, _slug = _m.group(1), _m.group(2)
    _slug = _reT.sub(r"[^a-z0-9\-]", "", _slug.lower()) or "ticket"
    _fname = "%s-%s.jpg" % (_fecha, _slug)
    _dir = _osT.path.join(_base, "static", "img", "comprobantes")
    _osT.makedirs(_dir, exist_ok=True)
    _dest = _osT.path.join(_dir, _fname)

    # Guardar (convertir a JPEG si PIL está disponible)
    try:
        _raw = _f.read()
        _saved = False
        try:
            from PIL import Image as _ImT
            import io as _ioT
            _img = _ImT.open(_ioT.BytesIO(_raw))
            if _img.mode in ("RGBA", "LA", "P"):
                _img = _img.convert("RGB")
            _img.save(_dest, "JPEG", quality=90)
            _saved = True
        except ImportError:
            pass
        if not _saved:
            with open(_dest, "wb") as _wf:
                _wf.write(_raw)
    except Exception as _e:
        return jsonify({"ok": False, "error": "No se pudo guardar: %s" % str(_e)[:120]}), 500

    # Marcar en plays-pending.json
    _rel = "img/comprobantes/%s" % _fname
    try:
        _play["comprobante"] = _rel
        with open(_pp_path, "w", encoding="utf-8") as _fh:
            _jsonT.dump({"fecha": _fecha, "plays": _plays}, _fh,
                        ensure_ascii=False, indent=2)
    except Exception as _e:
        return jsonify({"ok": False, "error": "Ticket guardado pero no se marcó: %s" % str(_e)[:120]}), 500

    return jsonify({"ok": True, "comprobante": _rel, "play_id": _play_id})


@app.route("/admin/wgt-tracker")
@login_required
def admin_wgt_tracker():
    """Tracker privado de WGT (5 meses) — solo Alex, en el Admin Dashboard."""
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    return render_template("admin_wgt_tracker.html")

@app.route("/admin/subir-ticket", methods=["GET", "POST"])
@login_required
def admin_subir_ticket():
    """Alex sube los tickets originales desde el panel admin."""
    import os, time
    from werkzeug.utils import secure_filename
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))

    program, plays = load_data()
    plays_sin_ticket = [p for p in plays if not p.get("comprobante")]

    pending_dir = os.path.join(app.static_folder, "img", "tickets-pending")
    os.makedirs(pending_dir, exist_ok=True)

    if request.method == "POST":
        play_id = (request.form.get("play_id") or "").strip()
        f = request.files.get("ticket")
        if not play_id or not f or not f.filename:
            flash("Elige la jugada y la foto del ticket.", "error")
            return redirect(url_for("admin_subir_ticket"))
        play = next((p for p in plays if p.get("id") == play_id), None)
        if not play:
            flash("Jugada no encontrada.", "error")
            return redirect(url_for("admin_subir_ticket"))
        ext = os.path.splitext(secure_filename(f.filename))[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        fname = f"{play_id}-{int(time.time())}{ext}"
        f.save(os.path.join(pending_dir, fname))
        # Registrar en el log para que el cron lo detecte
        try:
            import json as _json
            log_path = os.path.join(os.path.dirname(__file__), "data",
                                    "tickets-pendientes.json")
            try:
                with open(log_path) as _f:
                    _log = _json.load(_f)
            except Exception:
                _log = []
            _log.append({"play_id": play_id,
                         "pick": play.get("pick", ""),
                         "game": play.get("game", ""),
                         "archivo": fname,
                         "subido": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "procesado": False})
            with open(log_path, "w") as _f:
                _json.dump(_log, _f, indent=2)
        except Exception:
            pass
        db = get_db()
        log_notificacion(db, "ticket_subido", f"Ticket uploaded: {play.get('pick', play_id)}", play_id)
        flash(f"Ticket recibido para {play.get('pick', play_id)}. Lo verifico y lo publico.", "ok")
        return redirect(url_for("admin_subir_ticket"))

    # Lista de pendientes
    pendientes = []
    try:
        import json as _json
        log_path = os.path.join(os.path.dirname(__file__), "data",
                                "tickets-pendientes.json")
        with open(log_path) as _f:
            _log = _json.load(_f)
        for t in _log:
            if not t.get("procesado"):
                pendientes.append({
                    "play_pick": t.get("pick", ""),
                    "play_game": t.get("game", ""),
                    "subido": t.get("subido", ""),
                    "url": url_for("static", filename=f"img/tickets-pending/{t['archivo']}"),
                })
    except Exception:
        pass

    return render_template("admin_subir_ticket.html",
                           plays_sin_ticket=plays_sin_ticket,
                           pendientes=pendientes)


@app.route("/api/tickets-pendientes")
def api_tickets_pendientes():
    """Devuelve los tickets subidos sin procesar (para el cron de vigilancia)."""
    import json as _json, os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        return jsonify({"error": "unauthorized"}), 401
    try:
        log_path = os.path.join(os.path.dirname(__file__), "data",
                                "tickets-pendientes.json")
        if os.path.exists(log_path):
            with open(log_path) as _f:
                _log = _json.load(_f)
            return jsonify({"pendientes": [t for t in _log if not t.get("procesado")]})
        return jsonify({"pendientes": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat-pendientes")
def api_chat_pendientes():
    """Mensajes del chat embebido del admin sin leer (para el cron de vigilancia)."""
    import json as _json, os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        # Fallback: leer de push-keys.env como hacen los scripts
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "goals",
                                   "programa-de-apuestas-deportivas", "hidden_files",
                                   "push-keys.env")) as _f:
                for _line in _f:
                    if _line.strip().startswith("PUSH_TRIGGER_KEY="):
                        _key = _line.strip().split("=", 1)[1]
                        break
                else:
                    _key = ""
            if request.headers.get("X-Push-Key") != _key:
                return jsonify({"error": "unauthorized"}), 401
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
    try:
        log_path = os.path.join(os.path.dirname(__file__), "data", "admin-chat", "mensajes.jsonl")
        pendientes = []
        if os.path.exists(log_path):
            with open(log_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    _m = _json.loads(_line)
                    if not _m.get("leido"):
                        pendientes.append(_m)
        return jsonify({"pendientes": pendientes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat-marcar-leidos", methods=["POST"])
def api_chat_marcar_leidos():
    """Marca mensajes del chat admin como leídos."""
    import json as _json, os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "goals",
                                   "programa-de-apuestas-deportivas", "hidden_files",
                                   "push-keys.env")) as _f:
                for _line in _f:
                    if _line.strip().startswith("PUSH_TRIGGER_KEY="):
                        _key = _line.strip().split("=", 1)[1]
                        break
                else:
                    _key = ""
            if request.headers.get("X-Push-Key") != _key:
                return jsonify({"error": "unauthorized"}), 401
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
    try:
        log_path = os.path.join(os.path.dirname(__file__), "data", "admin-chat", "mensajes.jsonl")
        if not os.path.exists(log_path):
            return jsonify({"ok": True, "marcados": 0})
        lineas = []
        marcados = 0
        with open(log_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                _m = _json.loads(_line)
                if not _m.get("leido"):
                    _m["leido"] = True
                    marcados += 1
                lineas.append(_json.dumps(_m))
        with open(log_path, "w") as _f:
            _f.write("\n".join(lineas) + "\n" if lineas else "")
        return jsonify({"ok": True, "marcados": marcados})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/marcar-notificado", methods=["POST"])
def api_marcar_notificado():
    import json as _json, os
    key_ok = request.headers.get("X-Push-Key") == os.environ.get("PUSH_TRIGGER_KEY", "")
    if not key_ok:
        try:
            kf = os.path.join(os.path.dirname(__file__), "..", "goals",
                              "programa-de-apuestas-deportivas", "hidden_files", "push-keys.env")
            _key = ""
            with open(kf) as _f:
                for _line in _f:
                    if _line.strip().startswith("PUSH_TRIGGER_KEY="):
                        _key = _line.strip().split("=", 1)[1]
                        break
            if request.headers.get("X-Push-Key") != _key:
                return jsonify({"error": "unauthorized"}), 401
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
    ts = (request.get_json(silent=True) or {}).get("ts", "")
    try:
        notif_path = os.path.join(os.path.dirname(__file__), "data", "admin-chat", "notificar.jsonl")
        if not os.path.exists(notif_path):
            return jsonify({"ok": False, "error": "no file"}), 404
        lines = []
        updated = 0
        with open(notif_path) as _f:
            for _line in _f:
                s = _line.strip()
                if not s:
                    continue
                _m = _json.loads(s)
                if _m.get("ts") == ts and not _m.get("notificado"):
                    _m["notificado"] = True
                    updated += 1
                lines.append(_json.dumps(_m))
        with open(notif_path, "w") as _f:
            _f.write(chr(10).join(lines) + chr(10))
        return jsonify({"ok": True, "updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tickets-notificar")
def api_tickets_notificar():
    """Notificaciones de tickets procesados pendientes de avisar a Alex en el chat."""
    import json as _json, os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "goals",
                                   "programa-de-apuestas-deportivas", "hidden_files",
                                   "push-keys.env")) as _f:
                for _line in _f:
                    if _line.strip().startswith("PUSH_TRIGGER_KEY="):
                        _key = _line.strip().split("=", 1)[1]
                        break
                else:
                    _key = ""
            if request.headers.get("X-Push-Key") != _key:
                return jsonify({"error": "unauthorized"}), 401
        except Exception:
            return jsonify({"error": "unauthorized"}), 401
    try:
        notif_path = os.path.join(os.path.dirname(__file__), "data", "admin-chat", "notificar.jsonl")
        pendientes = []
        if os.path.exists(notif_path):
            with open(notif_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    _m = _json.loads(_line)
                    if not _m.get("notificado"):
                        pendientes.append(_m)
        return jsonify({"pendientes": pendientes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/values-record")
def api_values_record():
    """Récord del tracker VALUE del sistema (protegido con X-Push-Key).

    Lo usa el asistente para el botón 💎 Value plays del chat.
    Devuelve: wins, losses, profit, record y las values activas (pendientes).
    """
    import os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        return jsonify({"error": "unauthorized"}), 401
    try:
        db = get_db()
        rows = db.execute(
            "SELECT pick, cuota, fecha, resultado, stake_monto FROM tracked_plays "
            "WHERE user_id = ? AND nivel = 'VALUE' ORDER BY fecha DESC, id DESC",
            (VALUE_SYSTEM_USER_ID,),
        ).fetchall()
        wins = losses = 0
        profit = 0.0
        active = []
        history = []
        for r in rows:
            d = dict(r)
            p = play_profit_dollars({"cuota": d["cuota"], "stake_monto": d["stake_monto"] or 50.0,
                                     "resultado": d["resultado"]})
            if d["resultado"] == "W":
                wins += 1
                profit += p
                history.append({"pick": d["pick"], "cuota": d["cuota"], "fecha": d["fecha"],
                                "resultado": "W", "profit": round(p, 2)})
            elif d["resultado"] == "L":
                losses += 1
                profit += p
                history.append({"pick": d["pick"], "cuota": d["cuota"], "fecha": d["fecha"],
                                "resultado": "L", "profit": round(p, 2)})
            else:
                active.append({"pick": d["pick"], "cuota": d["cuota"], "fecha": d["fecha"]})
        return jsonify({
            "wins": wins, "losses": losses,
            "record": f"{wins}-{losses}",
            "profit": round(profit, 2),
            "profit_display": fmt_big_dollars(profit),
            "positive": profit >= 0,
            "active": active[:10],
            "history": history[:10],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/values")
@admin_required
def values_page():
    """Página del Value Tracker (2026-10-11, pedido por Alex: verlo en el Dashboard cuando quiera)."""
    db = get_db()
    rows = db.execute(
        "SELECT pick, cuota, fecha, resultado, stake_monto FROM tracked_plays "
        "WHERE user_id = ? AND nivel = 'VALUE' ORDER BY fecha DESC, id DESC",
        (VALUE_SYSTEM_USER_ID,),
    ).fetchall()
    wins = losses = 0
    profit = 0.0
    risked = 0.0
    active = []
    history = []
    for r in rows:
        d = dict(r)
        # Stake real de cada jugada (2026-10-12, Alex: $100 desde el 11-oct, $50 antes)
        stake = float(d["stake_monto"] or 100.0)
        p = play_profit_dollars({"cuota": d["cuota"], "stake_monto": stake,
                                 "resultado": d["resultado"]})
        cuota = d["cuota"] or 0
        cuota_str = f"+{cuota}" if cuota > 0 else str(cuota)
        if d["resultado"] == "W":
            wins += 1
            profit += p
            risked += stake
            history.append({"pick": d["pick"], "cuota_str": cuota_str, "fecha": d["fecha"],
                            "resultado": "W", "profit_str": fmt_money(p)})
        elif d["resultado"] == "L":
            losses += 1
            profit += p
            risked += stake
            history.append({"pick": d["pick"], "cuota_str": cuota_str, "fecha": d["fecha"],
                            "resultado": "L", "profit_str": fmt_money(p)})
        else:
            active.append({"pick": d["pick"], "cuota_str": cuota_str, "fecha": d["fecha"],
                           "resultado": "P", "profit_str": ""})
    settled = wins + losses
    win_rate = round(wins / settled * 100) if settled else 0
    roi = (profit / risked * 100) if risked else 0
    # Últimas 4: activas primero, luego historial
    all_plays = active + sorted(history, key=lambda x: x["fecha"], reverse=True)
    last4 = all_plays[:4]
    # Bankroll simulado: $10,000 inicial, 1% por jugada (2026-10-11, Alex)
    START_BANKROLL = 10000.0
    current_bankroll = START_BANKROLL + profit
    return render_template("values.html",
                           profit=profit,
                           profit_str=fmt_money(profit),
                           settled_count=settled,
                           pending_count=len(active),
                           win_rate=win_rate,
                           wins=wins, losses=losses,
                           roi_str=f"{roi:+.1f}%",
                           current_bankroll_str=fmt_money(current_bankroll),
                           last4=last4)


@app.route("/values/all")
@admin_required
def values_all():
    """Todas las jugadas VALUE registradas (2026-10-12, pedido por Alex: link 'View all')."""
    db = get_db()
    rows = db.execute(
        "SELECT pick, cuota, fecha, resultado, stake_monto FROM tracked_plays "
        "WHERE user_id = ? AND nivel = 'VALUE' ORDER BY fecha DESC, id DESC",
        (VALUE_SYSTEM_USER_ID,),
    ).fetchall()
    plays = []
    for r in rows:
        d = dict(r)
        p = play_profit_dollars({"cuota": d["cuota"], "stake_monto": 100.0,
                                 "resultado": d["resultado"]})
        cuota = d["cuota"] or 0
        cuota_str = f"+{cuota}" if cuota > 0 else str(cuota)
        res = d["resultado"]
        plays.append({"pick": d["pick"], "cuota_str": cuota_str, "fecha": d["fecha"],
                      "resultado": res,
                      "profit_str": fmt_money(p) if res in ("W", "L") else ""})
    return render_template("values_all.html", plays=plays, total=len(plays))

@app.route("/admin/chat-mensaje", methods=["POST"])
@login_required
def admin_chat_mensaje():
    """Alex sube tickets desde el admin: se guardan y se adjuntan a las jugadas de hoy."""
    import os, time, json as _json
    from datetime import datetime
    from werkzeug.utils import secure_filename
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        return jsonify({"ok": False, "error": "no autorizado"}), 403
    mensaje = request.form.get("mensaje", "").strip()
    hoy = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Guardar tickets en comprobantes (formato del flujo oficial)
    comp_dir = os.path.join(os.path.dirname(__file__), "static", "img", "comprobantes")
    os.makedirs(comp_dir, exist_ok=True)
    chat_dir = os.path.join(os.path.dirname(__file__), "data", "admin-chat")
    os.makedirs(chat_dir, exist_ok=True)
    guardados = []
    _batch_id = None
    # Siguiente índice libre del día: los tickets llegan en POSTs separados
    # (uno por ticket desde el admin) — numerar por lo que ya existe en disco
    # evita que el ticket 2 sobreescriba al ticket 1 (bug real 2026-10-09).
    try:
        _exist = [n for n in os.listdir(comp_dir)
                  if n.startswith(f"{hoy}-ticket-") and n.endswith(".jpg")]
    except Exception:
        _exist = []
    _idx = len(_exist) + 1
    for f in request.files.getlist("tickets"):
        if f and f.filename:
            # Nombre oficial: YYYY-MM-DD-ticket-N.jpg (único por día)
            nombre = f"{hoy}-ticket-{_idx}.jpg"
            _idx += 1
            f.save(os.path.join(comp_dir, nombre))
            guardados.append(f"img/comprobantes/{nombre}")
    # Auto-adjuntar a las jugadas de hoy (en orden)
    actualizadas = 0
    _matched = []
    if guardados:
        for fname in ["plays-pending.json", "plays.json"]:
            pp = os.path.join(os.path.dirname(__file__), "data", fname)
            try:
                with open(pp) as _f:
                    _data = _json.load(_f)
                if isinstance(_data, dict) and _data.get("fecha") == hoy:
                    _plays = _data.get("plays", [])
                    for j, _p in enumerate(_plays):
                        if j < len(guardados) and not _p.get("comprobante"):
                            _p["comprobante"] = guardados[j]
                            actualizadas += 1
                            if not any(m.get("id") == _p.get("id") for m in _matched):
                                _matched.append({
                                    "id": _p.get("id", ""),
                                    "pick": _p.get("pick", ""),
                                    "cuota_texto": _p.get("cuota_texto", ""),
                                    "stake_monto": _p.get("stake_monto", ""),
                                    "liga": _p.get("liga", ""),
                                    "hora": _p.get("game_time_et") or _p.get("hora", ""),
                                })
                    with open(pp, "w") as _f:
                        _json.dump(_data, _f, indent=2)
            except Exception:
                pass
    # Log para el watcher
    if mensaje or guardados:
        log_path = os.path.join(chat_dir, "mensajes.jsonl")
        with open(log_path, "a") as lf:
            lf.write(_json.dumps({
                "ts": ts,
                "mensaje": mensaje,
                "tickets": guardados,
                "actualizadas": actualizadas,
                "leido": True,  # Ya procesado automáticamente
            }) + "\n")
        try:
            log_notificacion(db, "admin_chat", "Tickets de Alex procesados",
                             f"{len(guardados)} ticket(s), {actualizadas} carta(s) actualizada(s)")
        except Exception:
            pass
        # Guardar para notificar a Alex en el chat principal
        try:
            notif_path = os.path.join(os.path.dirname(__file__), "data", "admin-chat", "notificar.jsonl")
            with open(notif_path, "a") as nf:
                nf.write(_json.dumps({
                    "ts": ts,
                    "tickets": len(guardados),
                    "actualizadas": actualizadas,
                    "notificado": False,
                }) + "\n")
        except Exception:
            pass
        # PUBLICAR EN GITHUB: si se adjuntaron tickets, commitear para que el
        # Dashboard los muestre (Alex 2026-10-09 - el ticket debe verse en la web)
        _push_ok = False
        if actualizadas > 0:
            try:
                import subprocess
                repo_dir = os.path.dirname(__file__)
                # Agregar los archivos modificados
                subprocess.run(["git", "add", "data/plays.json", "data/plays-pending.json"],
                               cwd=repo_dir, capture_output=True, timeout=10)
                # Agregar los tickets nuevos
                for g in guardados:
                    # g es como "img/comprobantes/2026-10-09-ticket-1.jpg"
                    # el path real es static/img/comprobantes/...
                    fpath = os.path.join("static", g)
                    subprocess.run(["git", "add", fpath],
                                   cwd=repo_dir, capture_output=True, timeout=10)
                # Commit
                subprocess.run(
                    ["git", "commit", "-m", f"Admin: ticket adjuntado {hoy} ({actualizadas} carta(s))"],
                    cwd=repo_dir, capture_output=True, timeout=15)
                # Push (best-effort, si falla no bloquea la respuesta).
                # Se captura el resultado: es la señal REAL del paso
                # "Dashboard updated" del panel de procesamiento.
                _push_ok = (subprocess.run(
                    ["git", "push", "origin", "master"],
                    cwd=repo_dir, capture_output=True, timeout=30).returncode == 0)
            except Exception:
                pass
        # ESTADO DEL PANEL DE PROCESAMIENTO (2026-10-14, diseño aprobado por
        # Alex): registro por lote con los pasos REALES del pipeline. El panel
        # del Admin lo lee por polling (/admin/ticket-status/<id>) y el
        # procesador del flujo (agente/crons) marca verifying y alerts vía
        # /admin/ticket-status-update. Ningún paso se simula aquí: received =
        # archivos guardados en ESTE request; matched = comprobante escrito en
        # la jugada; dashboard = carta actualizada + push a GitHub confirmado.
        if guardados:
            try:
                import secrets as _secrets
                _batch_id = "%s-%s" % (ts, _secrets.token_hex(3))
                _now_s = time.strftime("%Y-%m-%d %H:%M:%S")
                if actualizadas > 0:
                    _matched_step = {"state": "done", "at": _now_s,
                                     "detail": "Ticket adjuntado a la jugada"}
                    if _push_ok:
                        _dash_step = {"state": "done", "at": _now_s,
                                      "detail": "Carta en vivo en el Dashboard · guardado en GitHub"}
                    else:
                        _dash_step = {"state": "error", "at": _now_s,
                                      "detail": "La carta se actualizó en el servidor, pero el guardado en GitHub falló — no sobreviviría un reinicio"}
                else:
                    _matched_step = {"state": "error", "at": _now_s,
                                     "detail": "Ninguna jugada de hoy esperaba ticket — el archivo quedó guardado sin adjuntar"}
                    _dash_step = {"state": "error", "at": _now_s,
                                  "detail": "Sin carta que actualizar"}
                _batch = {
                    "id": _batch_id, "fecha": hoy, "ts": _now_s,
                    "epoch": time.time(),
                    "tickets": guardados, "actualizadas": actualizadas,
                    "plays": _matched,
                    "steps": {
                        "received": {"state": "done", "at": _now_s,
                                     "detail": "%d archivo(s) en el servidor" % len(guardados)},
                        "verifying": {"state": "pending"},
                        "matched": _matched_step,
                        "dashboard": _dash_step,
                        "alerts": {"state": "pending"},
                    },
                }
                _all = _ticket_status_todos()
                _all[_batch_id] = _batch
                if len(_all) > 20:
                    for _k in sorted(_all.keys())[:len(_all) - 20]:
                        _all.pop(_k, None)
                _ticket_status_guardar(_all)
            except Exception:
                _batch_id = None
    return jsonify({"ok": True, "tickets": len(guardados),
                    "actualizadas": actualizadas, "batch_id": _batch_id})


def _ticket_status_file():
    return os.path.join(os.path.dirname(__file__), "data", "admin-chat",
                        "ticket-status.json")


def _ticket_status_todos():
    import json as _json
    try:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS ticket_status_kv "
                   "(id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT)")
        row = db.execute(
            "SELECT data FROM ticket_status_kv WHERE id = 1").fetchone()
        if row and row["data"]:
            _d = _json.loads(row["data"])
            if isinstance(_d, dict) and _d:
                return _d
    except Exception:
        pass
    try:
        with open(_ticket_status_file()) as _f:
            _d = _json.load(_f)
        return _d if isinstance(_d, dict) else {}
    except Exception:
        return {}


def _ticket_status_guardar(_all):
    # Persiste en la BD (sobrevive redeploys); el archivo queda como espejo.
    import json as _json
    ok = False
    try:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS ticket_status_kv "
                   "(id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT)")
        db.execute("INSERT INTO ticket_status_kv (id, data) VALUES (1, ?) "
                   "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                   (_json.dumps(_all),))
        db.commit()
        ok = True
    except Exception:
        pass
    try:
        with open(_ticket_status_file(), "w") as _f:
            _json.dump(_all, _f, indent=2)
        ok = True
    except Exception:
        pass
    return ok


@app.route("/admin/ticket-status/latest")
def admin_ticket_status_latest():
    """Último lote de tickets (el panel del Admin retoma el estado al
    recargar la página). Auth: sesión admin o X-Admin-Key."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    _all = _ticket_status_todos()
    if not _all:
        return jsonify({"error": "sin lotes"}), 404
    return jsonify(_all[sorted(_all.keys())[-1]])


@app.route("/admin/ticket-status/<batch_id>")
def admin_ticket_status(batch_id):
    """Estado REAL de un lote de tickets (polling del panel).
    Auth: sesión admin o X-Admin-Key."""
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    _b = _ticket_status_todos().get(batch_id)
    if not _b:
        return jsonify({"error": "lote no encontrado"}), 404
    return jsonify(_b)


@app.route("/admin/ticket-status-update", methods=["POST"])
def admin_ticket_status_update():
    """El procesador del pipeline marca los pasos que ocurren FUERA del
    endpoint de subida: verifying (precio/monto leídos del ticket contra la
    jugada) y alerts (Telegram + push enviados). También puede corregir
    matched/dashboard si el flujo los completa por otra vía.
    Auth: sesión admin o X-Admin-Key.
    Body JSON: {batch_id, step, state, detail}."""
    import json as _json
    from datetime import datetime as _dtS
    if not _admin_api_autorizado():
        return jsonify({"error": "admin only"}), 403
    _d = request.get_json(silent=True) or {}
    _bid = (_d.get("batch_id") or "").strip()
    _step = (_d.get("step") or "").strip()
    _state = (_d.get("state") or "").strip()
    if _step not in ("received", "verifying", "matched", "dashboard", "alerts"):
        return jsonify({"error": "step inválido"}), 400
    if _state not in ("pending", "working", "done", "error"):
        return jsonify({"error": "state inválido"}), 400
    _all = _ticket_status_todos()
    _b = _all.get(_bid)
    if not _b:
        return jsonify({"error": "lote no encontrado"}), 404
    _b.setdefault("steps", {})[_step] = {
        "state": _state,
        "detail": (_d.get("detail") or "")[:200],
        "at": _dtS.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not _ticket_status_guardar(_all):
        return jsonify({"error": "no se pudo guardar el estado"}), 500
    return jsonify({"ok": True, "batch_id": _bid, "step": _step, "state": _state})

@login_required
def admin_accion(accion):
    """Alex dispara manualmente una acción automática desde el panel."""
    import json as _json, os, time
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        return jsonify({"error": "unauthorized"}), 401
    validas = {"email-jugadas", "telegram", "publicar-todo", "publish-plays"}
    if accion not in validas:
        return jsonify({"error": "acción no válida"}), 400
    try:
        queue_path = os.path.join(os.path.dirname(__file__), "data",
                                  "acciones-pendientes.json")
        try:
            with open(queue_path) as _f:
                _q = _json.load(_f)
        except Exception:
            _q = []
        _q.append({"accion": accion,
                   "pedido": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "ejecutado": False})
        with open(queue_path, "w") as _f:
            _json.dump(_q, _f, indent=2)
        if accion == "email-jugadas":
            log_notificacion(db, "email", "Plays email queued", "Will be sent to all members")
        if accion == "publish-plays":
            log_notificacion(db, "publish", "Publish ordered by Alex", "Today's plays queued for publication")
        if accion == "publicar-todo":
            # Un solo botón: email + Dashboard de miembros (pedido Alex 2026-10-08)
            _q.append({"accion": "email-jugadas",
                       "pedido": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "ejecutado": False})
            _q.append({"accion": "publish-plays",
                       "pedido": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "ejecutado": False})
            with open(queue_path, "w") as _f:
                _json.dump(_q, _f, indent=2)
            log_notificacion(db, "publish", "Publish to members ordered", "Email + Dashboard queued")
        return jsonify({"ok": True, "accion": accion})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log-notificacion", methods=["POST"])
def api_log_notificacion():
    """Los trabajos automáticos del agente registran notificaciones aquí.
    Protegido con X-Push-Key == PUSH_TRIGGER_KEY (2026-10-07, pedido por Alex:
    'todo lo que tú hagas debe enviar la notificación con el desglose')."""
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    tipo = (data.get("tipo") or "info")[:30]
    titulo = (data.get("titulo") or "")[:200]
    detalle = (data.get("detalle") or "")[:500]
    if not titulo:
        return jsonify({"error": "titulo requerido"}), 400
    db = get_db()
    log_notificacion(db, tipo, titulo, detalle)
    return jsonify({"ok": True})


@app.route("/api/acciones-pendientes")
def api_acciones_pendientes():
    """Devuelve las acciones pedidas por Alex sin ejecutar (para el cron)."""
    import json as _json, os
    if request.headers.get("X-Push-Key") != os.environ.get("PUSH_TRIGGER_KEY", ""):
        return jsonify({"error": "unauthorized"}), 401
    try:
        queue_path = os.path.join(os.path.dirname(__file__), "data",
                                  "acciones-pendientes.json")
        if os.path.exists(queue_path):
            with open(queue_path) as _f:
                _q = _json.load(_f)
            return jsonify({"pendientes": [a for a in _q if not a.get("ejecutado")]})
        return jsonify({"pendientes": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/miembros")
@login_required
def admin_miembros():
    """Lista privada de miembros — solo para Alex (is_admin=1)."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    _barrer_trials_vencidos(db)
    miembros = db.execute(
        "SELECT id, nombre, email, created_at, platinum_unlocked, is_admin, "
        "stake_fijo_elite, stake_fijo_gold, cancel_requested_at, stripe_customer_id, "
        "stripe_subscription_id, stripe_past_due, platinum_trial_ends, "
        "telegram_user_id, telegram_ban_pending, telegram_unban_pending "
        "FROM users "
        "WHERE LOWER(email) NOT IN ('thesharpteam8@gmail.com', 'system@value.local') "
        "AND email_verified = 1 "
        "ORDER BY created_at DESC"
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
    # ── 🔥 ACTIVE & VERIFIED (2026-10-20, pedido por Alex): miembros con el
    # correo confirmado y actividad en 2+ días distintos, con TODO lo que
    # el sistema sabe de ellos. La cuenta de Alex (dueño) no cuenta aquí.
    activos = []
    try:
        filas = db.execute(
            "SELECT u.id, u.nombre, u.email, u.activated_at, u.phone, "
            "u.sms_optin, u.telegram_user_id, u.stripe_customer_id, "
            "u.stripe_subscription_id, u.platinum_trial_ends, "
            "COUNT(*) AS entradas, "
            "COUNT(DISTINCT substr(a.created_at, 1, 10)) AS dias, "
            "MAX(a.created_at) AS ultima "
            "FROM users u JOIN member_activity a ON a.user_id = u.id "
            "WHERE u.email_verified = 1 "
            "AND LOWER(u.email) NOT IN ('thesharpteam8@gmail.com', "
            "'system@value.local', 'pena.manuel@myyahoo.com') "
            "GROUP BY u.id, u.nombre, u.email, u.activated_at, u.phone, "
            "u.sms_optin, u.telegram_user_id, u.stripe_customer_id, "
            "u.stripe_subscription_id, u.platinum_trial_ends "
            "HAVING COUNT(DISTINCT substr(a.created_at, 1, 10)) >= 2 "
            "ORDER BY entradas DESC"
        ).fetchall()
        _meses = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        for f in filas:
            d = dict(f)
            d["confirmado_txt"] = ""
            if d.get("activated_at"):
                try:
                    fa = datetime.fromisoformat(
                        str(d["activated_at"]).replace("Z", "+00:00"))
                    d["confirmado_txt"] = (
                        f"{_meses[fa.month - 1]} {fa.day}, {fa.year}")
                except Exception:
                    d["confirmado_txt"] = str(d["activated_at"])[:10]
            d["ultima_txt"] = ""
            if d.get("ultima"):
                try:
                    fu = datetime.fromisoformat(
                        str(d["ultima"]).replace("Z", "+00:00"))
                    if fu.tzinfo is None:
                        fu = fu.replace(tzinfo=timezone.utc)
                    fe = fu.astimezone(ZoneInfo("America/New_York"))
                    hora = fe.strftime("%I:%M %p").lstrip("0")
                    d["ultima_txt"] = (
                        f"{_meses[fe.month - 1]} {fe.day} · {hora}")
                except Exception:
                    d["ultima_txt"] = str(d["ultima"])[:10]
            d["ciudad"] = ""
            try:
                act0 = db.execute(
                    "SELECT ip FROM member_activity WHERE user_id = ?"
                    " AND ip IS NOT NULL AND ip != ''"
                    " ORDER BY created_at ASC LIMIT 1", (d["id"],)).fetchone()
                if act0:
                    ip0 = act0["ip"] if not isinstance(act0, dict) \
                        else act0.get("ip")
                    if ip0:
                        d["ciudad"] = _ciudad_de_ip(db, ip0) or ""
            except Exception:
                d["ciudad"] = ""
            try:
                tr = db.execute(
                    "SELECT COUNT(*) AS n FROM tracked_plays"
                    " WHERE user_id = ?", (d["id"],)).fetchone()
                d["tracked_n"] = (
                    tr["n"] if not isinstance(tr, dict)
                    else tr.get("n")) or 0
            except Exception:
                d["tracked_n"] = 0
            if d.get("stripe_subscription_id") or d.get("stripe_customer_id"):
                d["stripe_txt"] = "Customer · Paid"
                d["stripe_gold"] = False
            elif d.get("platinum_trial_ends"):
                d["stripe_txt"] = (
                    "No payment · Free month until "
                    + str(d["platinum_trial_ends"])[:10])
                d["stripe_gold"] = True
            else:
                d["stripe_txt"] = "No payment"
                d["stripe_gold"] = False
            d["sms_txt"] = "Yes" if d.get("sms_optin") else "No"
            d["telegram_txt"] = (
                "Linked" if d.get("telegram_user_id") else "Not linked")
            activos.append(d)
    except Exception:
        activos = []
    return render_template("admin_miembros.html", miembros=lista,
                           activos=activos,
                           eventos=[dict(e) for e in eventos])


@app.route("/admin/miembros/<int:user_id>")
@login_required
def admin_miembro_detalle(user_id):
    """Ficha completa de un miembro (2026-10-19, pedido por Alex): el panel
    /admin/miembros es solo la lista de nombres con «Ver más»; toda la
    información del miembro y su actividad en el programa vive aquí."""
    me = current_user()
    if not _admin_o_cuenta_alex(me):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    db = get_db()
    _barrer_trials_vencidos(db)
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin_miembros"))
    m = dict(row)
    # Ciudad de registro: primera IP de actividad (mismo criterio que
    # /api/new-members).
    ciudad = ""
    try:
        act0 = db.execute(
            "SELECT ip FROM member_activity WHERE user_id = ?"
            " AND ip IS NOT NULL AND ip != ''"
            " ORDER BY created_at ASC LIMIT 1", (user_id,)).fetchone()
        if act0:
            ip0 = act0["ip"] if not isinstance(act0, dict) else act0.get("ip")
            if ip0:
                ciudad = _ciudad_de_ip(db, ip0) or ""
    except Exception:
        ciudad = ""
    # Días que entró al Dashboard (checkins) + actividad reciente.
    dias_dashboard = 0
    ultimo_dia = ""
    try:
        ck = db.execute(
            "SELECT COUNT(*) AS n, MAX(fecha) AS u FROM checkins WHERE user_id = ?",
            (user_id,)).fetchone()
        if ck:
            dias_dashboard = ck["n"] if not isinstance(ck, dict) else (ck.get("n") or 0)
            ultimo_dia = (ck["u"] if not isinstance(ck, dict) else ck.get("u")) or ""
    except Exception:
        pass
    try:
        actividad = [dict(r) for r in db.execute(
            "SELECT kind, created_at FROM member_activity WHERE user_id = ?"
            " ORDER BY created_at DESC LIMIT 20", (user_id,)).fetchall()]
    except Exception:
        actividad = []
    # Tracker personal: resumen + últimas jugadas con su profit.
    trows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC LIMIT 100",
        (user_id,)).fetchall()
    tracked = [dict(r) for r in trows]
    for r in tracked:
        try:
            r["profit"] = play_profit_dollars(r)
        except Exception:
            r["profit"] = 0.0
    stats = compute_stats(tracked) if tracked else {}
    return render_template(
        "admin_miembro_detalle.html", m=m, ciudad=ciudad,
        dias_dashboard=dias_dashboard, ultimo_dia=ultimo_dia,
        actividad=actividad, tracked=tracked[:10], stats=stats)


@app.route("/admin/notificaciones/limpiar", methods=["POST"])
@login_required
def admin_notificaciones_limpiar():
    """Limpia todas las notificaciones (solo Alex/admin)."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    try:
        db.execute("DELETE FROM notificaciones")
        db.commit()
        flash("Notifications cleared.", "ok")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_notificaciones"))


@app.route("/admin/notificaciones")
@login_required
def admin_notificaciones():
    """Centro de notificaciones del admin (2026-10-07, pedido por Alex):
    todo lo que pasa — miembros nuevos, tickets, pagos."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    try:
        rows = db.execute(
            "SELECT id, tipo, titulo, detalle, created_at, leida FROM notificaciones "
            "ORDER BY id DESC LIMIT 100"
        ).fetchall()
        notifs = [dict(r) for r in rows]
    except Exception:
        notifs = []
    # Marcar como leídas al verlas
    try:
        db.execute("UPDATE notificaciones SET leida = 1 WHERE leida = 0")
        db.commit()
    except Exception:
        pass
    # Contar no leídas para el badge (antes de marcar)
    return render_template("admin_notificaciones.html", notificaciones=notifs)


@app.route("/admin/trakeos")
@login_required
def admin_trakeos():
    """Quién trakeó cada jugada publicada — solo para Alex (2026-10-05, pedido por Alex)."""
    db = get_db()
    user = current_user()
    if not _admin_o_cuenta_alex(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    rows = db.execute(
        "SELECT t.play_id, t.fecha, t.nivel, t.pick, t.cuota, t.resultado, "
        "t.stake_monto, t.created_at, u.nombre, u.email "
        "FROM tracked_plays t JOIN users u ON u.id = t.user_id "
        "WHERE t.user_id != 0 AND t.nivel != 'VALUE' "
        "ORDER BY t.fecha DESC, t.created_at DESC"
    ).fetchall()
    jugadas = []
    idx = {}
    for r in rows:
        d = dict(r)
        key = (d["play_id"], d["fecha"])
        if key not in idx:
            idx[key] = {"play_id": d["play_id"], "fecha": d["fecha"],
                        "nivel": d["nivel"], "pick": d["pick"],
                        "cuota": d["cuota"], "miembros": []}
            jugadas.append(idx[key])
        idx[key]["miembros"].append(d)
    return render_template("admin_trakeos.html", jugadas=jugadas)


@app.route("/resultados")
@login_required
def resultados():
    # RETIRADO 2026-10-12 (orden de Alex): solo quedan DOS trackers activos —
    # el EJECUTIVO del programa (/official-tracker) y el de VALUES. El tracker
    # clásico de miembros se elimina; todo link viejo aterriza en el ejecutivo.
    db = get_db()
    record_checkin(db, session["user_id"])
    return redirect(url_for("official_tracker"))


@app.route("/ev-board")
@login_required
def ev_board():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("ev_board.html", board=load_ev_board())


@app.route("/ev-board/results")
@login_required
def ev_board_results():
    """EV Board Results (Platinum, 2026-10-19): los edges del board que el
    sistema auto-trackea (nivel VALUE) ya calificados: record, profit y
    ROI reales del tracker del sistema, sin numeros inventados."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return render_template(
            "ev_board_results_locked.html",
            upsell_cta_url=url_for("desbloquear_platinum"),
        )
    rows = db.execute(
        "SELECT pick, cuota, fecha, resultado, stake_monto FROM tracked_plays "
        "WHERE user_id = ? AND nivel = 'VALUE' ORDER BY fecha DESC, id DESC",
        (VALUE_SYSTEM_USER_ID,),
    ).fetchall()
    wins = losses = 0
    profit = 0.0
    risked = 0.0
    plays = []
    for r in rows:
        d = dict(r)
        stake = float(d["stake_monto"] or 100.0)
        p = play_profit_dollars({"cuota": d["cuota"], "stake_monto": stake,
                                 "resultado": d["resultado"]})
        cuota = d["cuota"] or 0
        cuota_str = f"+{cuota}" if cuota > 0 else str(cuota)
        res = d["resultado"]
        if res == "W":
            wins += 1
            profit += p
            risked += stake
        elif res == "L":
            losses += 1
            profit += p
            risked += stake
        plays.append({"pick": d["pick"], "cuota_str": cuota_str,
                      "fecha": d["fecha"], "resultado": res,
                      "profit_str": fmt_money(p) if res in ("W", "L") else ""})
    settled = wins + losses
    win_rate = round(wins / settled * 100) if settled else 0
    roi = (profit / risked * 100) if risked else 0
    return render_template(
        "ev_board_results.html",
        wins=wins, losses=losses, settled=settled,
        pending=len(plays) - settled,
        win_rate=win_rate,
        profit_str=fmt_money(profit),
        profit_positive=profit >= 0,
        roi_str=f"{roi:+.1f}%",
        plays=plays[:100],
    )


@app.route("/sharp")
@login_required
def sharp():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("sharp.html", board=load_sharp_board())


@app.route("/parlay-builder")
@login_required
def parlay_builder():
    """Parlay Builder — Platinum members only. Free members see the upsell."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return render_template(
            "parlay_builder_locked.html",
            upsell_cta_url=url_for("desbloquear_platinum"),
        )
    return render_template("parlay_builder.html", games=load_parlay_games())


# ----------------------------- AI Assistant (Platinum only) -----------------------------
# Rule-based assistant: answers from real program data only (no external LLM).
# Intents: best plays, parlay suggestion, today's official plays, record, bankroll.

def _ai_fmt_odds(price):
    try:
        p = int(float(price))
    except (TypeError, ValueError):
        return "—"
    return f"+{p}" if p > 0 else str(p)


def _ai_to_decimal(odds):
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    if odds == 0 or (-100 < odds < 100):
        return None
    return 1 + odds / 100.0 if odds > 0 else 1 + 100.0 / abs(odds)


def _ai_to_american(dec):
    if dec is None or dec <= 1:
        return "—"
    if dec >= 2.0:
        return f"+{int(round((dec - 1) * 100))}"
    return str(-int(round(100 / (dec - 1))))


def _ai_best_edges(limit=5, sport=None):
    """Top edges from the +EV board, sorted by EV%. Optional sport filter."""
    try:
        board = load_ev_board() or {}
    except Exception:
        return []
    edges = [e for e in (board.get("edges") or []) if isinstance(e, dict)]
    if sport:
        edges = [e for e in edges if str(e.get("sport") or "").upper() == sport]
    edges.sort(key=lambda e: float(e.get("ev_pct") or 0), reverse=True)
    return edges[:max(1, limit)]


_AI_LEAGUE_ALIASES = (
    ("nfl", "NFL"),
    ("ncaaf", "NCAAF"),
    ("college football", "NCAAF"),
    ("mlb", "MLB"),
    ("baseball", "MLB"),
    ("nhl", "NHL"),
    ("hockey", "NHL"),
    ("nba", "NBA"),
    ("basketball", "NBA"),
)


def _ai_detect_sport(q):
    for alias, sport in _AI_LEAGUE_ALIASES:
        if alias in q:
            return sport
    return None


def _ai_answer(question, user):
    """Keyword intent matching. Returns (answer_text, cards). Never invents data."""
    q = (question or "").strip().lower()
    cards = []

    def _edge_line(e):
        live = " · LIVE" if e.get("live") else ""
        return f"• {e.get('team', '?')} ({_ai_fmt_odds(e.get('price'))}) — {float(e.get('ev_pct') or 0):.1f}% edge{live}"

    def _edge_card(e):
        return {
            "title": str(e.get("team") or "?"),
            "sub": f"{e.get('game', '')} · {e.get('market', '')}".strip(" ·"),
            "badge": f"{_ai_fmt_odds(e.get('price'))} · {float(e.get('ev_pct') or 0):.1f}% EV",
        }

    # ---- parlay suggestion ----
    if any(k in q for k in ("parlay", "parley", "combo", "multi-leg", "accumulator")):
        sport = _ai_detect_sport(q)
        legs = []
        seen_games = set()
        for e in _ai_best_edges(12, sport=sport):
            if str(e.get("market", "")).lower() != "moneyline":
                continue
            if e.get("live"):
                continue
            g = str(e.get("game") or "")
            if g in seen_games:
                continue
            dec = _ai_to_decimal(e.get("price"))
            if dec is None:
                continue
            seen_games.add(g)
            legs.append(e)
            if len(legs) == 3:
                break
        if len(legs) < 2:
            scope = f" for {sport}" if sport else ""
            return (f"I don't have enough moneyline edges{scope} right now to build a parlay. Check the +EV Board for the latest.", [])
        combined = 1.0
        for e in legs:
            combined *= _ai_to_decimal(e.get("price"))
        lines = [f"Leg {i+1}: {e.get('team')} ({_ai_fmt_odds(e.get('price'))})" for i, e in enumerate(legs)]
        scope = f" ({sport})" if sport else ""
        answer = ("Here's a {n}-leg moneyline parlay{scope} from today's top edges:\n".format(n=len(legs), scope=scope)
                  + "\n".join(lines)
                  + f"\n\nCombined odds: {_ai_to_american(combined)}")
        cards = [_edge_card(e) for e in legs]
        return (answer, cards)

    # ---- best plays / top edges ----
    _best_kw = any(k in q for k in ("best play", "best edge", "top edge", "top play", "value play",
                                   "what should i bet", "what to bet", "recommend", "suggestion",
                                   "sharpest", "highest ev"))
    _best_kw = _best_kw or (
        ("best" in q or "top" in q)
        and any(k in q for k in ("bet", "play", "edge", "pick"))
    )
    if _best_kw:
        sport = _ai_detect_sport(q)
        edges = _ai_best_edges(5, sport=sport)
        if not edges:
            scope = f" for {sport}" if sport else ""
            return (f"The +EV Board has no edges{scope} right now. Check back later — it refreshes throughout the day.", [])
        lines = [_edge_line(e) for e in edges]
        scope = f" ({sport})" if sport else ""
        answer = f"Top edges on the +EV Board right now{scope}:\n" + "\n".join(lines)
        cards = [_edge_card(e) for e in edges]
        return (answer, cards)

    # ---- today's official plays ----
    if any(k in q for k in ("today", "official", "card", "tonight", "plays")):
        try:
            plays = load_plays() or []
        except Exception:
            plays = []
        if not plays:
            return ("No official plays are published yet today. The card drops once tickets are verified.", [])
        lines = []
        for p in plays:
            lines.append(f"• {p.get('pick', '?')} ({_ai_fmt_odds(p.get('cuota'))}) · {p.get('liga', '')} · {p.get('hora', '')}".rstrip(" ·"))
        answer = "Today's official plays:\n" + "\n".join(lines)
        return (answer, cards)

    # ---- record ----
    if any(k in q for k in ("record", "how are we", "profit", "win rate", "roi", "stats",
                            "performance", "results", "doing")):
        try:
            s = program_stats() or {}
        except Exception:
            s = {}
        if not s:
            return ("I couldn't load the program record right now. Try the Full Record page.", [])
        answer = (f"Program record: {s.get('ganadas', 0)}–{s.get('perdidas', 0)} "
                  f"({s.get('win_rate', 0)}% win rate), {s.get('profit_display', '$0')} profit, "
                  f"{s.get('roi', 0)}% ROI.")
        return (answer, cards)

    # ---- bankroll ----
    if any(k in q for k in ("bankroll", "my stake", "unit size", "how much", "1u", "stake")):
        try:
            br = float(user["bankroll"]) if user and user["bankroll"] else None
        except (KeyError, IndexError, TypeError, ValueError):
            br = None
        if br:
            answer = f"Your bankroll is ${br:,.2f}. At 1% staking, 1u = ${br * 0.01:,.2f}."
        else:
            answer = "You don't have a bankroll set. Add one in Account so I can size your stakes (1u = 1% of bankroll)."
        return (answer, cards)

    # ---- help / greeting ----
    if any(k in q for k in ("help", "what can you", "hello", "hi", "hey", "hola")):
        return ("I'm The Sharp Team assistant. Ask me for:\n"
                "• \"best plays\" — top +EV edges right now\n"
                "• \"build me a parlay\" — a 2–3 leg parlay from top edges\n"
                "• \"today's plays\" — today's official card\n"
                "• \"record\" — program record and profit\n"
                "• \"bankroll\" — your bankroll and unit size", [])

    # ---- fallback ----
    return ("I can help with: best plays, parlay suggestions, today's official plays, program record, and your bankroll. "
            "What do you need?", [])


@app.route("/ai-assistant")
@login_required
def ai_assistant():
    """AI Assistant — Platinum members only. Free members see the upsell."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return render_template(
            "ai_assistant_locked.html",
            upsell_cta_url=url_for("desbloquear_platinum"),
        )
    return render_template("ai_assistant.html")




@app.route("/api/ai-ask", methods=["POST"])
@login_required
def api_ai_ask():
    """JSON Q&A endpoint for the AI Assistant. Platinum only."""
    user = current_user()
    if not platinum_unlocked_for(user):
        return jsonify({"error": "platinum_required"}), 403
    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "")[:500]
    if not question.strip():
        return jsonify({
            "answer": "Ask me something — try \"best plays\" or \"build me a parlay\".",
            "cards": [],
        })
    answer, cards = _ai_answer(question, user)
    return jsonify({"answer": answer, "cards": cards})


@app.route("/props")
@login_required
def props():
    """Player Props Board (Platinum, 2026-10-19): sin Platinum activo,
    el miembro cae en la página de desbloqueo."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return redirect(url_for("desbloquear_platinum"))
    return render_template("props.html", board=load_props_board())


# ----------------------------- Free public tools (no login) ----------------
@app.route("/tools/parlay-calculator")
def tools_parlay_calculator():
    """Calculadora de parlays 100% del lado del cliente. Pública, sin login."""
    return render_template("tools_parlay_calculator.html")


@app.route("/tools/calculators")
def tools_calculators():
    """Hub público de calculadoras: conversor, no-vig, Kelly, hedge y parlay.

    100% del lado del cliente, sin login (innovación aprobada por Alex
    2026-10-16 del inventario de herramientas de referencia).
    """
    return render_template("tools_calculators.html")


@app.route("/tools")
@login_required
def tools():
    """Panel All Tools (2026-10-19, diseno aprobado por Alex estilo
    sheet): buscador, recientes del dispositivo (localStorage) y
    candado Platinum en las herramientas de pago."""
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template(
        "tools.html",
        es_platinum=platinum_unlocked_for(current_user()),
        desbloquear_url=url_for("desbloquear_platinum"),
    )


@app.route("/odds-board")
@login_required
def odds_board():
    """Odds Board (Platinum, 2026-10-19): juegos de hoy con el mejor
    moneyline por lado y la casa que lo da. Datos de la rama data-board."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return render_template(
            "odds_board_locked.html",
            upsell_cta_url=url_for("desbloquear_platinum"),
        )
    board = load_odds_board()
    return render_template("odds_board.html",
                           games=board.get("games", []),
                           updated_at=board.get("updated_at", ""))


@app.route("/tools/line-moves")
@login_required
def tools_line_moves():
    """Sharp Line Movement (Platinum, 2026-10-19): sin sesión cae en
    login; sin Platinum activo, en la página de desbloqueo. Datos de
    la rama data-board."""
    db = get_db()
    record_checkin(db, session["user_id"])
    if not platinum_unlocked_for(current_user()):
        return redirect(url_for("desbloquear_platinum"))
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
    # Un título "" explícito se respeta (alertas en vivo estilo Action:
    # iOS pone el nombre de la app una sola vez; un título con la marca
    # la duplica con su línea "from Sharp Team"). Solo si falta la
    # clave o viene null se usa el default de la marca.
    _t = data.get("title")
    title = _t if _t is not None else "The Sharp Team"
    body = data.get("body") or "New +EV edge on the board."
    url = data.get("url") or "/ev-board"
    image = data.get("image") or ""
    # Filtro por preferencia (pedido Alex 2026-10-08, estilo WGT):
    # notif_type mapea al campo notif_* del usuario.
    notif_type = data.get("notif_type") or "notif_ev"
    allowed_types = {"notif_plays", "notif_graded", "notif_live", "notif_reminder", "notif_ev", "notif_price", "notif_sharp", "notif_masterclass"}
    if notif_type not in allowed_types:
        notif_type = "notif_ev"
    payload = json.dumps({"title": title, "body": body, "url": url, "image": image})
    db = get_db()
    # Targeting opcional por email (2026-10-13): si el payload trae "emails",
    # el push va SOLO a esos miembros (ej. alertas en vivo solo para Alex).
    emails = data.get("emails") or []
    if emails:
        ph = ",".join("?" for _ in emails)
        subs = db.execute(
            f"SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps"
            f" JOIN users u ON u.id = ps.member_id WHERE u.{notif_type} = 1"
            f" AND lower(u.email) IN ({ph})",
            [str(e).lower() for e in emails],
        ).fetchall()
    else:
        # Exclusión opcional (2026-10-14): "exclude_emails" deja fuera a esos
        # miembros del envío general (ej. las alertas en vivo de miembros no
        # deben duplicar el push personal de Alex, que va por "emails").
        exclude = data.get("exclude_emails") or []
        if exclude:
            ph = ",".join("?" for _ in exclude)
            subs = db.execute(
                f"SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps"
                f" JOIN users u ON u.id = ps.member_id WHERE u.{notif_type} = 1"
                f" AND lower(u.email) NOT IN ({ph})",
                [str(e).lower() for e in exclude],
            ).fetchall()
        else:
            subs = db.execute(
                f"SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps"
                f" JOIN users u ON u.id = ps.member_id WHERE u.{notif_type} = 1"
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


@app.route("/api/version")
def api_version():
    """Versión actual del deploy (para el aviso de 'nueva versión disponible')."""
    from flask import jsonify
    return jsonify({"v": ASSET_V})

@app.route("/api/recent-results")
@login_required
def api_recent_results():
    """Últimos resultados W/L para la burbuja flotante de miembros (2026-10-07,
    pedido por Alex): burbuja que aparece al ganar/perder y desaparece en 5s."""
    from flask import jsonify, request
    # Modo prueba: ?test=1
    if request.args.get("test") == "1":
        return jsonify({"results": [{
            "id": "test-braves-001",
            "pick": "Braves ML (+141)",
            "resultado": "L",
            "nivel": "ELITE",
            "logo": "https://a.espncdn.com/i/teamlogos/mlb/500/atl.png",
            "equipo": "Atlanta Braves",
        }]})
    from datetime import datetime, timezone, timedelta
    resultados = []
    try:
        _, plays = load_data()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for p in (plays or []):
            if p.get("resultado") not in ("W", "L"):
                continue
            # Solo recientes: usa graded_at si existe, si no la fecha del play
            try:
                ts = p.get("graded_at") or p.get("fecha", "")
                # Si no hay timestamp confiable, incluirlo igual (mejor mostrar que perder)
                abbr = (p.get("abbr") or "").lower()
                liga = (p.get("liga") or "").upper()
                logo = ""
                if abbr and liga in ("MLB", "NFL", "NBA", "NHL"):
                    logo = f"https://a.espncdn.com/i/teamlogos/{liga.lower()}/500/{abbr}.png"
                resultados.append({
                    "id": p.get("id"),
                    "pick": p.get("pick"),
                    "resultado": p.get("resultado"),
                    "nivel": p.get("nivel"),
                    "logo": logo,
                    "equipo": p.get("equipo"),
                })
            except Exception:
                continue
    except Exception:
        pass
    return jsonify({"results": resultados[-5:]})


@app.route("/api/new-subscribers")
def api_new_subscribers():
    """Burbujita de nuevos suscriptores (2026-10-07, pedido por Alex).
    Solo la cuenta de miembro de Alex puede verla. Devuelve los miembros
    registrados en las últimas 24h (máx 5)."""
    from flask import jsonify
    user = current_user()
    if not is_alex_member(user):
        return jsonify({"subscribers": []})
    # Modo prueba: ?test=1 devuelve un suscriptor demo
    if request.args.get("test") == "1":
        return jsonify({"subscribers": [{
            "nombre": "Carlos M.",
            "email": "carlos.m@email.com",
            "plan": "Free",
            "created_at": "just now",
        }]})
    subs = []
    try:
        db = get_db()
        rows = db.execute(
            """SELECT nombre, email, platinum_unlocked, created_at FROM users
               WHERE email != ? AND created_at >= datetime('now', '-24 hours')
               ORDER BY created_at DESC LIMIT 5""",
            (ALEX_MEMBER_EMAIL,),
        ).fetchall()
        for r in rows:
            subs.append({
                "nombre": r["nombre"] or "Member",
                "email": r["email"] or "",
                "plan": "Platinum" if r["platinum_unlocked"] else "Free",
                "created_at": r["created_at"] or "",
            })
    except Exception:
        pass
    return jsonify({"subscribers": subs})

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
    """Miembros CONFIRMADOS. ?since=ISO (created_at) filtra desde esa marca.

    Sin verificar no es miembro (2026-10-19, orden de Alex): esta lista
    solo incluye cuentas con el correo confirmado (email_verified = 1) y
    ordena por la fecha de ACTIVACIÓN — el aviso de miembro nuevo llega
    cuando la persona confirma, no cuando llena el formulario.
    Lo lee el cron de la VM cada 10 min. Protegido con
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
            "SELECT id, nombre, email, created_at, activated_at, platinum_unlocked FROM users"
            " WHERE email_verified = 1 AND created_at >= ? ORDER BY COALESCE(activated_at, created_at) ASC, id ASC",
            (since,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, nombre, email, created_at, activated_at, platinum_unlocked FROM users"
            " WHERE email_verified = 1"
            " ORDER BY COALESCE(activated_at, created_at) DESC, id DESC LIMIT 500"
        ).fetchall()
    members = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        # Ciudad de registro (pedido de Alex 2026-10-17: la tarjeta de
        # nuevo miembro lleva la ciudad de donde se registro): se toma la
        # IP de su primera actividad (el login automatico al registrarse)
        # y se resuelve con el geo-cache de la plataforma.
        ciudad = ""
        try:
            act = db.execute(
                "SELECT ip FROM member_activity WHERE user_id = ?"
                " AND ip IS NOT NULL AND ip != ''"
                " ORDER BY created_at ASC LIMIT 1",
                (d["id"],),
            ).fetchone()
            if act:
                ip0 = act["ip"] if not isinstance(act, dict) else act.get("ip")
                if ip0:
                    ciudad = _ciudad_de_ip(db, ip0) or ""
        except Exception:
            ciudad = ""
        members.append(
            {
                "id": d["id"],
                "nombre": d.get("nombre"),
                "email": d.get("email"),
                "created_at": d.get("created_at"),
                "activated_at": d.get("activated_at") or d.get("created_at"),
                "elite": bool(d.get("platinum_unlocked")),
                "ciudad": ciudad,
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
    # 2026-10-01: los links expiran a los 7 días en Telegram; los vencidos sin
    # usar se purgan para que el conteo sea real y el cron reponga.
    from datetime import timedelta as _td
    limite = (datetime.now(timezone.utc) - _td(days=7)).isoformat(timespec="seconds")
    db.execute(
        "DELETE FROM telegram_invite_links WHERE used = 0 AND created_at <= ?",
        (limite,),
    )
    db.commit()
    row = db.execute(
        "SELECT COUNT(*) AS n FROM telegram_invite_links WHERE used = 0"
    ).fetchone()
    n = row["n"] if row else 0
    return jsonify({"unused": n})


@app.route("/api/telegram-invite", methods=["GET"])
def api_telegram_invite():
    """Entrega al miembro Platinum el link permanente de invitación al canal Platinum.

    2026-10-02: link permanente directo (sin expiración ni límite de
    miembros); reemplaza el pool de un solo uso. Requiere sesión y Elite
    activa. El control de acceso lo hace el ban-sync: un baneado no puede
    entrar aunque tenga el link.
    """
    user = current_user()
    if user is None:
        return jsonify({"error": "login_required"}), 401
    if not platinum_unlocked_for(user):
        return jsonify({"error": "elite_required"}), 403
    return jsonify({"ok": True, "invite_link": TELEGRAM_VIP_INVITE_LINK})


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
        row = db.execute("SELECT id, nombre, platinum_unlocked, cancel_requested_at, telegram_user_id, welcome_email_sent FROM users WHERE email = ?",
                         (email,)).fetchone()
        if row:
            uid = row["id"]
            era_nuevo = not bool(row["platinum_unlocked"])
            estaba_cancelado = bool(row["cancel_requested_at"])
            tiene_tg = bool(row["telegram_user_id"])
            db.execute(
                "UPDATE users SET platinum_unlocked = 1, email_verified = 1, activation_token = NULL, stripe_customer_id = ?, "
                "stripe_subscription_id = ?, stripe_past_due = 0, cancel_requested_at = NULL, platinum_trial_ends = NULL WHERE id = ?",
                (customer_id or None, sub_id or None, uid),
            )
            # Inglés forzado en las páginas de Stripe del miembro.
            _stripe_force_english(customer_id)
            # Si tiene Telegram vinculado, el cron lo desbanea del canal Sharp Club
            # para que pueda reingresar. Se hace SIEMPRE al activar (no solo si
            # cancel_requested_at está marcado): el 2026-10-01 la cancelación se
            # hizo con el botón de prueba (no marca cancel_requested_at) y al
            # reactivar por Stripe el desbaneo jamás se encoló — Alex lo reclamó.
            # unbanChatMember es seguro aunque no esté baneado.
            if tiene_tg:
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
            # 2026-10-05: si ya lo recibió al registrarse (gratis o no),
            # no se repite.
            # 2026-10-08 (pedido Alex): si se había cancelado y se vuelve a
            # suscribir, le llega el WELCOME BACK (distinto al de primera vez).
            if estaba_cancelado:
                try:
                    send_welcome_back_email(row["nombre"] or "", email)
                    db.execute("UPDATE users SET welcome_email_sent = 1 WHERE id = ?", (uid,))
                    db.commit()
                except Exception:
                    pass
            elif era_nuevo and not bool(row["welcome_email_sent"]):
                try:
                    send_welcome_email(row["nombre"] or "", email)
                    db.execute("UPDATE users SET welcome_email_sent = 1 WHERE id = ?", (uid,))
                    db.commit()
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
            # Todos los links de pago fallido llevan a /cuenta#past-due (pedido por Alex
            # 2026-10-08): ahí está la sección past-due estilo WGT con el botón
            # "Update card & restore access" que sí abre la ventana de pago de Stripe.
            pay_url = "https://www.thesharpteam.bet/cuenta#past-due"
            nombre = ""
            if email:
                r = db.execute("SELECT nombre FROM users WHERE email = ?",
                               (email,)).fetchone()
                if r:
                    nombre = r["nombre"] or ""
                    # Marca past_due para el banner de dunning en /desbloquear-platinum
                    # (pedido Alex 2026-10-02, idea del ejemplo que envió).
                    db.execute("UPDATE users SET stripe_past_due = 1 WHERE email = ?",
                               (email,))
                    db.commit()
            ya_notificado = False
            if invoice_id:
                ya_notificado = bool(db.execute(
                    "SELECT 1 FROM stripe_events WHERE tipo = 'invoice.payment_failed'"
                    " AND detalle LIKE ? LIMIT 1",
                    (f"%{invoice_id}%",)).fetchone())
            if ya_notificado:
                _auditar(email, f"pago fallido invoice {invoice_id}: ya notificado antes")
            else:
                # Inglés forzado en las páginas de Stripe del miembro.
                _stripe_force_english(data.get("customer"))
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


from faq_bot import get_bot


@app.route("/api/faq-ask", methods=["POST"])
def api_faq_ask():
    """Chatbot de preguntas y respuestas: responde desde data/faq.json (sin login).

    Motor TF-IDF bilingüe (faq_bot.py): entiende preguntas libres en español
    o inglés y responde en el idioma de la pregunta. Sin APIs externas.
    """
    data = request.get_json(force=True, silent=True) or {}
    q = (data.get("question") or "").strip()[:300]
    answer, matched, _lang = get_bot().answer(q)
    return jsonify({"answer": answer, "matched": matched})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)


# ============================================================================
# VALUE SYSTEM TRACKING (2026-10-04, orden de Alex)
# "Las valúes se trakean las juegue yo o no."
# Las VALUE plays se trackean automáticamente a nivel SISTEMA (no personal).
# user_id=0 = usuario sistema para VALUE. No aparece en trackers personales.
# ============================================================================
VALUE_SYSTEM_USER_ID = 0


@app.route("/admin/track-value-auto", methods=["POST"])
def track_value_auto():
    """Auto-trackea una VALUE play al sistema (llamado por ev-board-refresh).
    Body JSON: {play_id, fecha, pick, cuota, edge, game, liga}
    Auth: sesión admin O header X-Push-Key (para crons).
    """
    from flask import Response, request
    import os
    # Auth: X-Push-Key (crons) o sesión admin
    push_key = request.headers.get("X-Push-Key", "")
    key_ok = push_key and push_key == os.environ.get("PUSH_TRIGGER_KEY", "")
    if not key_ok:
        # Fallback a sesión admin
        from flask import session
        user = None
        if "user_id" in session:
            db_chk = get_db()
            user = db_chk.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not (user and is_admin_for(user)):
            return Response("Admin access required.", status=403, mimetype="text/plain")
    db = get_db()
    ensure_value_system_user(db)
    data = request.get_json(force=True, silent=True) or {}
    
    play_id = data.get("play_id")
    if not play_id:
        return Response("play_id requerido", status=400, mimetype="text/plain")
    
    # Verificar si ya existe
    existing = db.execute(
        "SELECT id FROM tracked_plays WHERE user_id = ? AND play_id = ?",
        (VALUE_SYSTEM_USER_ID, play_id)
    ).fetchone()
    if existing:
        return Response(f"Ya trackeada: {play_id}", mimetype="text/plain")
    
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # NOTA: las columnas juego/liga las garantiza migrate_db() al arrancar
    # (chequeo seguro con information_schema + commit). NO hacer ALTER aquí:
    # en PostgreSQL un ALTER fallido (columna ya existe) aborta la transacción
    # y envenena el INSERT posterior -> HTTP 500 (incidente 2026-10-11 00:08 ET).
    db.execute(
        """INSERT INTO tracked_plays
           (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
            stake_monto, edge, resultado, juego, liga, created_at)
           VALUES (?, ?, ?, 'VALUE', ?, ?, 1.0, 100.0, ?, NULL, ?, ?, ?)""",
        (VALUE_SYSTEM_USER_ID, play_id, data.get("fecha", ""),
         data.get("pick", ""), int(data.get("cuota", 0)),
         _edge_num(data.get("edge")), data.get("game", ""), data.get("liga", ""), now)
    )
    db.commit()
    return Response(f"VALUE trackeada: {play_id}", mimetype="text/plain")


def _value_cron_auth():
    """Auth compartida para endpoints del cron de VALUE: X-Push-Key o sesión admin."""
    from flask import request, session
    import os
    push_key = request.headers.get("X-Push-Key", "")
    key_ok = push_key and push_key == os.environ.get("PUSH_TRIGGER_KEY", "")
    if key_ok:
        return True
    if "user_id" in session:
        db_chk = get_db()
        user = db_chk.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if user and is_admin_for(user):
            return True
    return False


@app.route("/admin/value-pending", methods=["GET"])
def value_pending():
    """Lista las VALUE plays pendientes (resultado NULL). Para el cron value-results-update.
    Auth: X-Push-Key (crons) o sesión admin."""
    from flask import Response
    import json as _json
    if not _value_cron_auth():
        return Response("Admin access required.", status=403, mimetype="text/plain")
    db = get_db()
    rows = db.execute(
        """SELECT play_id, fecha, pick, cuota, stake_monto, juego, liga
           FROM tracked_plays WHERE user_id = ? AND nivel = 'VALUE'
           AND (resultado IS NULL OR resultado = '')
           ORDER BY fecha DESC, id DESC""",
        (VALUE_SYSTEM_USER_ID,),
    ).fetchall()
    data = [dict(r) for r in rows]
    for _d in data:
        # Decimal de Postgres no es serializable a JSON (2026-10-21).
        if _d.get("stake_monto") is not None:
            _d["stake_monto"] = float(_d["stake_monto"])
    return Response(_json.dumps({"pending": data}, ensure_ascii=False),
                    mimetype="application/json")


@app.route("/admin/value-result", methods=["POST"])
def value_result():
    """Actualiza el resultado de una VALUE play (W/L). Para el cron value-results-update.
    Body JSON: {play_id, resultado: "W"|"L"}. Auth: X-Push-Key (crons) o sesión admin."""
    from flask import Response, request
    if not _value_cron_auth():
        return Response("Admin access required.", status=403, mimetype="text/plain")
    data = request.get_json(force=True, silent=True) or {}
    play_id = data.get("play_id")
    res = (data.get("resultado") or "").upper()
    if not play_id or res not in ("W", "L"):
        return Response("play_id y resultado W/L requeridos", status=400, mimetype="text/plain")
    db = get_db()
    cur = db.execute(
        "UPDATE tracked_plays SET resultado = ? WHERE user_id = ? AND play_id = ? "
        "AND (resultado IS NULL OR resultado = '')",
        (res, VALUE_SYSTEM_USER_ID, play_id),
    )
    db.commit()
    if cur.rowcount:
        return Response(f"Actualizada: {play_id} = {res}", mimetype="text/plain")
    return Response(f"Sin cambios (no pendiente o no existe): {play_id}", mimetype="text/plain")


@app.route("/admin/value-system-stats")
@admin_required
def value_system_stats():
    """Estadísticas del tracker SYSTEM de VALUE."""
    from flask import Response
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC",
        (VALUE_SYSTEM_USER_ID,)
    ).fetchall()
    data = [dict(r) for r in rows]
    for _d in data:
        # Decimal de Postgres: ni la aritmética ni json.dumps lo aceptan
        # mezclado con float (2026-10-21).
        for _k in ("stake_monto", "stake_unidades", "edge"):
            if _d.get(_k) is not None:
                _d[_k] = float(_d[_k])
    stats = stats_por_nivel(data)
    out = {
        "total": len(data),
        "value": stats.get("VALUE", {}),
        "plays": data,
    }
    return Response(json.dumps(out, indent=1, ensure_ascii=False),
                    mimetype="application/json")


def ensure_value_system_user(db):
    """Crea el usuario sistema para VALUE tracking si no existe."""
    row = db.execute("SELECT id FROM users WHERE id = ?", (VALUE_SYSTEM_USER_ID,)).fetchone()
    if not row:
        # Crear usuario sistema (id fijo 0)
        try:
            db.execute(
                "INSERT INTO users (id, nombre, email, password_hash, created_at) "
                "VALUES (?, 'VALUE System', 'system@value.local', '', ?)",
                (VALUE_SYSTEM_USER_ID, "2026-10-04 00:00:00")
            )
            db.commit()
        except Exception:
            db.rollback()
    return VALUE_SYSTEM_USER_ID


@app.route("/admin/backfill-value-2026-10-03")
@admin_required
def backfill_value_2026_10_03():
    """TEMPORAL: backfill de las 10 VALUE del sábado 2026-10-03 con resultados.
    6-4, +$151.00"""
    from flask import Response
    from datetime import datetime
    db = get_db()
    ensure_value_system_user(db)
    
    plays = [
        # (play_id, pick, cuota, edge, resultado)
        ("play-2026-10-03-missouri-value", "Missouri Tigers", 184, 0, "W"),
        ("play-2026-10-03-kentucky-value", "Kentucky Wildcats", 182, 0, "W"),
        ("play-2026-10-03-unlv-value", "UNLV Rebels", 122, 0, "W"),
        ("play-2026-10-03-louisville-value", "Louisville Cardinals", 250, 0, "L"),
        ("play-2026-10-03-nebraska-value", "Nebraska Cornhuskers", -150, 0, "W"),
        ("play-2026-10-03-tampabay-value", "Tampa Bay Rays", -130, 0, "W"),
        ("play-2026-10-03-southflorida-value", "South Florida Bulls", 180, 17.3, "L"),
        ("play-2026-10-03-texastech-value", "Texas Tech Red Raiders", -142, 13.6, "W"),
        ("play-2026-10-03-milwaukee-value", "Milwaukee Brewers -1.5", 135, 9.0, "L"),
        ("play-2026-10-03-washington-value", "Washington Huskies", 205, 8.0, "L"),
    ]
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for play_id, pick, cuota, edge, resultado in plays:
        existing = db.execute(
            "SELECT id FROM tracked_plays WHERE user_id = ? AND play_id = ?",
            (VALUE_SYSTEM_USER_ID, play_id)
        ).fetchone()
        if existing:
            # Actualizar resultado si no tiene
            db.execute(
                "UPDATE tracked_plays SET resultado = ? WHERE id = ? AND resultado IS NULL",
                (resultado, existing["id"])
            )
            continue
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, created_at)
               VALUES (?, ?, '2026-10-03', 'VALUE', ?, ?, 1.0, 50.0, ?, ?, ?)""",
            (VALUE_SYSTEM_USER_ID, play_id, pick, cuota, edge, resultado, now)
        )
        inserted += 1
    
    db.commit()
    return Response(f"Backfill completo: {inserted} nuevas, resto actualizadas",
                    mimetype="text/plain")


@app.route("/api/auto-track-alex", methods=["POST"])
def api_auto_track_alex():
    """Trackea automáticamente una jugada en el tracker personal de Alex
    (cuenta de miembro pena.manuel@myyahoo.com).

    Lo llama el asistente cuando Alex envía tickets originales
    (regla permanente 2026-10-05, pedido por Alex: "cuando yo te mande
    los ticket original inmediatamente dale a trake automáticamente").

    Protegido con header X-Push-Key == PUSH_TRIGGER_KEY
    (mismo patrón que /api/push-edge).

    JSON: {"play_id": str (requerido),
           "fecha": "YYYY-MM-DD",
           "nivel": "ELITE|GOLD",
           "pick": str,
           "cuota": int (americano),
           "stake_unidades": float,
           "stake_monto": float (opcional: si no viene se calcula con
               stake_personalizado, igual que el botón "+ Track"),
           "edge": float|null,
           "resultado": "W"|"L"|null (null = pendiente; la app lo liquida
               sola al ver el tracker/home),
           "comprobante": "img/comprobantes/....jpg"|null}

    Idempotente: si (user_id, play_id) ya existe no duplica; si viene
    resultado y el existente está pendiente, lo actualiza.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    play_id = (data.get("play_id") or "").strip()
    if not play_id:
        return jsonify({"error": "play_id requerido"}), 400
    resultado = data.get("resultado")
    if resultado not in ("W", "L", None):
        return jsonify({"error": "resultado debe ser W, L o null"}), 400
    db = get_db()
    alex = db.execute(
        "SELECT * FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL.lower(),)
    ).fetchone()
    if not alex:
        return jsonify({"error": "cuenta de Alex no encontrada"}), 404
    uid = alex["id"]
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    existing = db.execute(
        "SELECT id, resultado FROM tracked_plays WHERE user_id = ? AND play_id = ?",
        (uid, play_id),
    ).fetchone()
    if existing:
        if resultado and not existing["resultado"]:
            db.execute(
                "UPDATE tracked_plays SET resultado = ? WHERE id = ?",
                (resultado, existing["id"]),
            )
            db.commit()
            return jsonify({"ok": True, "action": "resultado_actualizado"})
        return jsonify({"ok": True, "action": "ya_existe"})
    play = {
        "nivel": data.get("nivel") or "",
        "stake_unidades": data.get("stake_unidades") or 0,
        "stake_monto": data.get("stake_monto") or 0,
    }
    stake_monto = data.get("stake_monto")
    if not stake_monto:
        stake_monto = stake_personalizado(play, alex)
    params = (
        uid,
        play_id,
        data.get("fecha") or datetime.now(TZ).strftime("%Y-%m-%d"),
        data.get("nivel") or "",
        data.get("pick") or "",
        int(data.get("cuota") or 0),
        float(data.get("stake_unidades") or 0),
        float(stake_monto or 0),
        _edge_num(data.get("edge")),
        resultado,
        data.get("comprobante"),
        now,
    )
    if db.use_pg:
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, comprobante, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            params,
        )
    else:
        db.execute(
            """INSERT OR IGNORE INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, comprobante, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
    db.commit()
    return jsonify({"ok": True, "action": "insertado"})


@app.route("/api/set-balance-alex", methods=["POST"])
def api_set_balance_alex():
    """Fija el balance de una casa en la cuenta personal de Alex
    (pena.manuel@myyahoo.com).

    Lo llama el asistente para sincronizaciones puntuales pedidas por Alex
    (2026-10-05: Novig a $802.88 = $500 base + $302.88 profit del tracker).
    De aquí en adelante el balance se mueve solo vía bump_book_balance al
    liquidarse cada jugada.

    Protegido con header X-Push-Key == PUSH_TRIGGER_KEY
    (mismo patrón que /api/auto-track-alex).

    JSON: {"book": "Novig", "balance": 802.88}
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    book = (data.get("book") or "").strip()[:40]
    if not book:
        return jsonify({"error": "book requerido"}), 400
    if data.get("delete") is True:
        db = get_db()
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
        ).fetchone()
        if not row:
            return jsonify({"error": "cuenta de Alex no encontrada"}), 404
        db.execute(
            "DELETE FROM book_balances WHERE user_id = ? AND book_name = ?",
            (row["id"], book),
        )
        db.commit()
        return jsonify({"ok": True, "action": "eliminado", "book": book})
    try:
        balance = round(float(data.get("balance")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "balance inválido"}), 400
    db = get_db()
    row = db.execute(
        "SELECT id FROM users WHERE LOWER(email) = ?", (ALEX_MEMBER_EMAIL,)
    ).fetchone()
    if not row:
        return jsonify({"error": "cuenta de Alex no encontrada"}), 404
    _ensure_book_balances_table(db)
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    ex = db.execute(
        "SELECT id FROM book_balances WHERE user_id = ? AND book_name = ?",
        (row["id"], book),
    ).fetchone()
    if ex:
        db.execute(
            "UPDATE book_balances SET balance = ?, updated_at = ? WHERE id = ?",
            (balance, now, ex["id"]),
        )
        action = "actualizado"
    else:
        db.execute(
            "INSERT INTO book_balances (user_id, book_name, balance, updated_at) VALUES (?, ?, ?, ?)",
            (row["id"], book, balance, now),
        )
        action = "creado"
    db.commit()
    return jsonify({"ok": True, "action": action, "book": book, "balance": balance})
# Force redeploy Sat Oct 10 11:17:29 EDT 2026
