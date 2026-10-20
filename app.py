#!/usr/bin/env python3
"""Portable tool inventory MVP. Python 3.10+, standard library only."""
from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse, urlencode

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TOOLSTOCK_DATA", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "photos"
DB_PATH = Path(os.environ.get("TOOLSTOCK_DB", DATA_DIR / "toolstock.sqlite3"))
HOST = os.environ.get("TOOLSTOCK_HOST", "0.0.0.0")
PORT = int(os.environ.get("TOOLSTOCK_PORT", "8000"))
STATES = {"new": "Новый", "used": "БУ — после работы", "sharpened": "После переточки", "regrind_queue": "К отправке на переточку", "regrind": "На переточке"}
AVAILABLE_STATES = ("new", "used", "sharpened")
MOVEMENT_LABELS = {"receipt":"Поступление","issue":"Выдача","return":"Возврат","writeoff_stock":"Списание со склада","writeoff_issued":"Списание у рабочего","regrind_send":"Отправка на переточку","regrind_receive":"Прием после переточки"}


def movement_label(kind,state_to=None):
    if kind=="return" and state_to=="regrind_queue": return "Передача в очередь на переточку"
    return MOVEMENT_LABELS.get(kind,kind)
ROLES = {"ADMIN": "Администратор", "STOREKEEPER": "Кладовщик", "WORKER": "Рабочий"}
PERMISSIONS = {
    "dashboard": "Обзор",
    "catalog.view": "Просмотр каталога",
    "catalog.edit": "Создание и изменение карточек",
    "transactions.manage": "Проведение складских операций",
    "reports.view": "Отчёты и выгрузки",
    "history.view": "Журнал действий",
    "purchasing.view": "Просмотр заявок на закупку",
    "purchasing.manage": "Создание заявок на закупку",
}
DEFAULT_ROLE_PERMISSIONS = {
    "ADMIN": list(PERMISSIONS),
    "STOREKEEPER": ["dashboard", "catalog.view", "catalog.edit" , "transactions.manage", "reports.view", "history.view", "purchasing.view", "purchasing.manage"],
    "WORKER": ["dashboard", "catalog.view", "reports.view"],
}
ITEM_CATEGORIES = {
    "consumable": ["Пластина", "Фреза", "Резьбофреза", "Сверло", "Метчик", "Плашка", "Развертка", "Резец", "Борштанга", "Зенкер", "Зенковка", "Пила", "Нож", "Абразив", "Цанга", "Прочее"],
    "fixture": ["Оправка", "Державка", "Втулка", "Патрон", "Цанговый патрон", "Цанга", "Расточная система", "Расточная головка", "Переходник", "Удлинитель", "Зажим", "Зажимное приспособление", "Тиски", "Призма", "Прочее"]
}
TOOL_PARAMETER_FIELDS = [("diameter", "Ø, мм"), ("l1", "l1, мм"), ("length", "L, мм"), ("z", "z — число зубьев"), ("shank", "d хвостовика, мм"), ("radius", "r, мм"), ("extra", "Другие параметры")]
BODY_TOOL_CATEGORIES={"фреза","резьбофреза","борштанга","зенкер","зенковка","резец","пила"}
THREAD_TOOL_CATEGORIES={"резьбофреза","метчик","плашка"}
AXIAL_TOOL_CATEGORIES={"сверло","метчик","развертка","зенкер","зенковка","резьбофреза"}
FIXTURE_CATEGORIES={"оправка","державка","втулка","патрон","цанговый патрон","цанга","расточная система","расточная головка","переходник","удлинитель","зажим","зажимное приспособление","тиски","призма"}
ISO_1832_FIELDS = [
    ("shape","Форма пластины",[(x,x) for x in "HOPSTCDEMVWLABKR"]),
    ("clearance","Задний угол",[(x+" — "+v,x) for x,v in [("A","3°"),("B","5°"),("C","7°"),("D","15°"),("E","20°"),("F","25°"),("G","30°"),("N","0°"),("P","11°"),("O","другой")]]),
    ("tolerance","Класс допуска",[(x,x) for x in "AFCH E GJKLMNU".replace(" ","")]),
    ("fixing","Отверстие / стружколом",[(x+" — "+v,x) for x,v in [("N","без стружколома"),("R","без отверстия, канавка с одной стороны"),("F","канавка с двух сторон"),("A","цилиндрическое отверстие, без канавки"),("M","отверстие, канавка с одной стороны"),("G","отверстие, канавка с двух сторон"),("W","отверстие с частичной зенковкой, без канавки"),("T","отверстие с частичной зенковкой, канавка"),("X","специальное исполнение")]]),
]
ISO_513_GROUPS={"P":"Стали","M":"Нержавеющие стали","K":"Чугуны","N":"Цветные металлы","S":"Жаропрочные сплавы и титан","H":"Закаленные материалы"}
MAX_BODY = 8 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def within_edit_window(occurred_at) -> bool:
    try:
        stamp=datetime.fromisoformat(str(occurred_at).replace("Z","+00:00"))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
        age=datetime.now(timezone.utc)-stamp.astimezone(timezone.utc)
        return timedelta(0)<=age<timedelta(hours=24)
    except (TypeError,ValueError): return False


def h(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def script_json(value):
    """JSON safe inside an HTML script element, including untrusted text."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def number(value):
    return float(str(value).strip().replace(",", "."))


def begin_write(con):
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")


def product_primary_name(product) -> str:
    """Main user-facing product label: manufacturer model, then common name."""
    return str(product["model"] or "").strip() or str(product["name"] or "").strip()


def product_secondary_name(product) -> str:
    model=str(product["model"] or "").strip()
    name=str(product["name"] or "").strip()
    return name if model and name.casefold()!=model.casefold() else str(product["category"] or "").strip()


def fmt_num(value) -> str:
    try:
        number=float(value or 0)
        return str(int(number)) if math.isfinite(number) and number.is_integer() else format(number,"g")
    except (TypeError,ValueError): return str(value or "0")


def has_permission(user, permission):
    if not user: return False
    if user["role"] == "ADMIN": return True
    try: permissions=json.loads(user["permissions_json"] or "[]")
    except (KeyError,IndexError,TypeError,ValueError): permissions=DEFAULT_ROLE_PERMISSIONS.get(user["role"],[])
    return permission in permissions


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.create_function("unicode_contains", 2, lambda value, term: str(term or "").casefold() in str(value or "").casefold(), deterministic=True)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, location TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS workers (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, personnel_no TEXT UNIQUE, machine_id INTEGER REFERENCES machines(id), active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL,
 worker_id INTEGER UNIQUE REFERENCES workers(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_definitions (
 code TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, permissions_json TEXT NOT NULL DEFAULT '[]', is_system INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS worker_machines (
 worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
 machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
 PRIMARY KEY(worker_id,machine_id)
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, item_class TEXT NOT NULL CHECK(item_class IN ('consumable','fixture')),
 category TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', manufacturer TEXT NOT NULL DEFAULT '',
 parameters TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'шт', min_qty REAL NOT NULL DEFAULT 0,
 inventory_no TEXT, params_json TEXT NOT NULL DEFAULT '{}', price_per_unit REAL NOT NULL DEFAULT 0,
 location TEXT NOT NULL DEFAULT '', photo TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, occurred_at TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
 kind TEXT NOT NULL CHECK(kind IN ('receipt','issue','return','writeoff_stock','writeoff_issued','regrind_send','regrind_receive')),
 qty REAL NOT NULL CHECK(qty > 0), state_from TEXT, state_to TEXT,
 worker_id INTEGER REFERENCES workers(id), machine_id INTEGER REFERENCES machines(id),
 actor_user_id INTEGER REFERENCES users(id), note TEXT NOT NULL DEFAULT '',
 voided_at TEXT, voided_by_user_id INTEGER REFERENCES users(id), void_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS movement_changes (
 id INTEGER PRIMARY KEY, movement_id INTEGER NOT NULL REFERENCES movements(id), changed_at TEXT NOT NULL,
 actor_user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, reason TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_movements_date ON movements(occurred_at);
CREATE INDEX IF NOT EXISTS ix_movements_product ON movements(product_id);
CREATE INDEX IF NOT EXISTS ix_movements_worker_machine ON movements(worker_id,machine_id);
CREATE TABLE IF NOT EXISTS submitted_forms (
 user_id INTEGER NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,request_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_requests (
 id INTEGER PRIMARY KEY, request_no TEXT UNIQUE, created_at TEXT NOT NULL,
 created_by INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL DEFAULT 'Создана'
);
CREATE TABLE IF NOT EXISTS purchase_request_lines (
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id), code TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
 qty INTEGER NOT NULL CHECK(qty > 0), unit TEXT NOT NULL, unit_price REAL NOT NULL DEFAULT 0,
 stock_at_request REAL NOT NULL, min_at_request REAL NOT NULL
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.executescript(SCHEMA)
        cols={r[1] for r in con.execute("PRAGMA table_info(products)")}
        if "inventory_no" not in cols: con.execute("ALTER TABLE products ADD COLUMN inventory_no TEXT")
        if "params_json" not in cols: con.execute("ALTER TABLE products ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'")
        if "price_per_unit" not in cols: con.execute("ALTER TABLE products ADD COLUMN price_per_unit REAL NOT NULL DEFAULT 0")
        purchase_cols={r[1] for r in con.execute("PRAGMA table_info(purchase_requests)")}
        for column,definition in (("closed_at","TEXT"),("closed_by","INTEGER REFERENCES users(id)"),("archived_at","TEXT"),("archived_by","INTEGER REFERENCES users(id)")):
            if column not in purchase_cols: con.execute(f"ALTER TABLE purchase_requests ADD COLUMN {column} {definition}")
        for code,label in ROLES.items():
            con.execute("INSERT OR IGNORE INTO role_definitions(code,name,permissions_json,is_system) VALUES(?,?,?,1)",(code,label,json.dumps(DEFAULT_ROLE_PERMISSIONS[code],ensure_ascii=False)))
        role_sql=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
        if "CHECK(role IN" in role_sql:
            con.execute("""CREATE TABLE users_migrated (
              id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL,
              worker_id INTEGER UNIQUE REFERENCES workers(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""")
            con.execute("INSERT INTO users_migrated SELECT id,username,password_hash,role,worker_id,active,created_at FROM users")
            con.execute("DROP TABLE users")
            con.execute("ALTER TABLE users_migrated RENAME TO users")
        con.execute("INSERT OR IGNORE INTO worker_machines(worker_id,machine_id) SELECT id,machine_id FROM workers WHERE machine_id IS NOT NULL")
        movement_sql=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='movements'").fetchone()[0]
        if "regrind_send" not in movement_sql:
            con.execute("""CREATE TABLE movements_new (
              id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, occurred_at TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
              kind TEXT NOT NULL CHECK(kind IN ('receipt','issue','return','writeoff_stock','writeoff_issued','regrind_send','regrind_receive')),
              qty REAL NOT NULL CHECK(qty > 0), state_from TEXT, state_to TEXT,
              worker_id INTEGER REFERENCES workers(id), machine_id INTEGER REFERENCES machines(id),
              actor_user_id INTEGER REFERENCES users(id), note TEXT NOT NULL DEFAULT '',
              voided_at TEXT, voided_by_user_id INTEGER REFERENCES users(id), void_reason TEXT NOT NULL DEFAULT '')""")
            con.execute("INSERT INTO movements_new(id,batch_id,occurred_at,product_id,kind,qty,state_from,state_to,worker_id,machine_id,actor_user_id,note) SELECT id,batch_id,occurred_at,product_id,kind,qty,state_from,state_to,worker_id,machine_id,actor_user_id,note FROM movements")
            con.execute("DROP TABLE movements")
            con.execute("ALTER TABLE movements_new RENAME TO movements")
        movement_cols={r[1] for r in con.execute("PRAGMA table_info(movements)")}
        if "voided_at" not in movement_cols: con.execute("ALTER TABLE movements ADD COLUMN voided_at TEXT")
        if "voided_by_user_id" not in movement_cols: con.execute("ALTER TABLE movements ADD COLUMN voided_by_user_id INTEGER REFERENCES users(id)")
        if "void_reason" not in movement_cols: con.execute("ALTER TABLE movements ADD COLUMN void_reason TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS ix_movements_date ON movements(occurred_at)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_movements_product ON movements(product_id)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_movements_worker_machine ON movements(worker_id,machine_id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_products_inventory_no ON products(inventory_no) WHERE inventory_no IS NOT NULL AND inventory_no<>''")
        for old in con.execute("SELECT id,model,params_json FROM products WHERE item_class='consumable' AND category='Резьбовая пластина'").fetchall():
            params=json.loads(old["params_json"] or "{}")
            params.setdefault("insert_kind","thread")
            params.setdefault("thread_insert_designation",old["model"] or "")
            con.execute("UPDATE products SET category='Пластина',params_json=? WHERE id=?",(json.dumps(params,ensure_ascii=False),old["id"]))
        con.execute("PRAGMA foreign_keys=ON")


def pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (salt.hex(), digest.hex())


def pw_check(password: str, saved: str) -> bool:
    try:
        scheme, iterations, salt, digest = saved.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return scheme == "pbkdf2_sha256" and hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def raw_balance(con: sqlite3.Connection, product_id: int, state: str, *, worker_id=None, machine_id=None) -> float:
    filt = ""
    if worker_id is not None:
        filt += " AND worker_id=?"
    if machine_id is not None:
        filt += " AND machine_id=?"
    # The stock expression is global; assigned balance uses only matching worker/machine rows.
    held = con.execute(f"""
      SELECT COALESCE(SUM(CASE WHEN kind='issue' AND state_to=? THEN qty
                               WHEN kind IN ('return','writeoff_issued') AND state_from=? THEN -qty ELSE 0 END),0)
      FROM movements WHERE product_id=? AND voided_at IS NULL {filt}
    """, [state, state, product_id] + ([worker_id] if worker_id is not None else []) + ([machine_id] if machine_id is not None else [])).fetchone()[0]
    stock = con.execute("""SELECT COALESCE(SUM(CASE
                           WHEN kind IN ('receipt','return') AND state_to=? THEN qty
                           WHEN kind IN ('regrind_send','regrind_receive') AND state_to=? THEN qty
                           WHEN kind IN ('issue','writeoff_stock') AND state_from=? THEN -qty
                           WHEN kind IN ('regrind_send','regrind_receive') AND state_from=? THEN -qty
                           ELSE 0 END),0) FROM movements WHERE product_id=? AND voided_at IS NULL""", (state, state, state, state, product_id)).fetchone()[0]
    return float(stock if worker_id is None and machine_id is None else held)


def balance(con: sqlite3.Connection, product_id: int, state: str, *, worker_id=None, machine_id=None) -> float:
    return raw_balance(con,product_id,state,worker_id=worker_id,machine_id=machine_id)


def ledger_errors(con,product_id):
    stock={state:0.0 for state in STATES}; held={}; errors=[]
    rows=con.execute("SELECT * FROM movements WHERE product_id=? AND voided_at IS NULL ORDER BY occurred_at,id",(product_id,))
    for row in rows:
        qty=float(row["qty"]); source=row["state_from"]; target=row["state_to"]
        if row["kind"] in ("receipt","return") and target in stock: stock[target]+=qty
        elif row["kind"] in ("issue","writeoff_stock") and source in stock: stock[source]-=qty
        elif row["kind"] in ("regrind_send","regrind_receive"):
            if source in stock: stock[source]-=qty
            if target in stock: stock[target]+=qty
        if row["worker_id"] is not None:
            key=(row["worker_id"],row["machine_id"])
            amounts=held.setdefault(key,{state:0.0 for state in STATES})
            if row["kind"]=="issue" and target in amounts: amounts[target]+=qty
            elif row["kind"] in ("return","writeoff_issued") and source in amounts: amounts[source]-=qty
        if any(value < -1e-9 for value in stock.values()) or any(value < -1e-9 for amounts in held.values() for value in amounts.values()):
            errors.append(row["id"]); break
    return errors


def warehouse_total(con, product_id: int) -> float:
    return sum(balance(con, product_id, state) for state in STATES)


def usable_total(con, product_id: int) -> float:
    return sum(balance(con, product_id, state) for state in AVAILABLE_STATES)


def held_total(con, product_id: int, worker_id=None, machine_id=None) -> float:
    return sum(balance(con, product_id, state, worker_id=worker_id, machine_id=machine_id) for state in STATES)


def stock_breakdown(con, product_ids):
    captions={"new":"Новое","used":"БУ","sharpened":"Переточено","regrind_queue":"К отправке","regrind":"На переточке"}
    amounts={state:sum(balance(con,pid,state) for pid in product_ids) for state in STATES}
    return "<small class=stock-states>"+" · ".join(f"{captions[state]}: {amounts[state]:g}" for state in STATES if amounts[state])+"</small>"


def movement(con, product_id, kind, qty, actor, *, state_from=None, state_to=None,
             worker_id=None, machine_id=None, note="", batch_id=None):
    con.execute("""INSERT INTO movements
      (batch_id,occurred_at,product_id,kind,qty,state_from,state_to,worker_id,machine_id,actor_user_id,note)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
      (batch_id or uuid.uuid4().hex, now_iso(), product_id, kind, qty, state_from, state_to,
       worker_id, machine_id, actor["id"], note.strip()))


def options(rows, selected=None, placeholder="Выберите") -> str:
    out = [f'<option value="">{h(placeholder)}</option>']
    for row in rows:
        label = row.get("label") if isinstance(row, dict) else None
        if label is None:
            label = row["name"]
        value = row.get("id") if isinstance(row, dict) else row["id"]
        out.append(f'<option value="{h(value)}"{" selected" if str(value)==str(selected) else ""}>{h(label)}</option>')
    return "".join(out)


def layout(title, body, user=None, notice="", *, active=""):
    nav = ""
    if user:
        links = []
        for url,label,key,permission in [("/dashboard","Главная","dashboard","dashboard"),("/catalog","Каталог инструмента","catalog","catalog.view"),("/transactions","Учет и движение","transactions","transactions.manage"),("/purchasing","Заявки","purchasing","purchasing.view"),("/reports","Отчёты","reports","reports.view"),("/history","История операций","history","history.view")]:
            if has_permission(user,permission) or (key=="history" and has_permission(user,"transactions.manage")): links.append((url,label,key))
        if user["role"] == "ADMIN": links.append(("/admin", "Справочники и доступ", "admin"))
        icons = {
            "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/>',
            "catalog": '<path d="M4 5.5 12 2l8 3.5v13L12 22l-8-3.5z"/><path d="m4.5 5.7 7.5 3.4 7.5-3.4M12 9v12"/>',
            "transactions": '<path d="M4 7h15l-3-3M20 17H5l3 3"/><path d="M19 7v4M5 17v-4"/>',
            "purchasing": '<path d="M7 3h10l3 3v15H4V3z"/><path d="M8 11h8M8 15h8M8 7h5"/>',
            "reports": '<path d="M4 19V5M4 19h17"/><path d="m7 15 4-4 3 2 6-7"/>',
            "history": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2M4 5v4h4"/>',
            "admin": '<circle cx="12" cy="8" r="3.5"/><path d="M5 21v-1.5a7 7 0 0 1 14 0V21z"/><path d="M19 8h3M20.5 6.5v3"/>',
        }
        groups = [("РАБОЧАЯ ОБЛАСТЬ", {"dashboard", "catalog", "transactions", "purchasing"}), ("АНАЛИТИКА", {"reports", "history"}), ("НАСТРОЙКА", {"admin"})]
        nav = '<aside class="sidebar"><button type="button" class="mobile-menu button secondary" aria-expanded="false" aria-controls="main-navigation">Меню</button><a class="brand" href="/dashboard"><span class="brand-logo-crop"><img src="/static/logo.png" alt="" width="68" height="68"></span><span>КАТЕКС<small>учёт инструмента</small></span></a><nav id="main-navigation" aria-label="Основная навигация">'
        for group_label, group_keys in groups:
            group_links = [link for link in links if link[2] in group_keys]
            if not group_links: continue
            nav += f'<div class="nav-group"><span class="nav-group-title">{group_label}</span>'
            nav += "".join(f'<a class="{"active" if active==key else ""}" href="{url}"{" aria-current=page" if active==key else ""}><svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true">{icons[key]}</svg><span>{label}</span></a>' for url,label,key in group_links)
            nav += '</div>'
        nav += f'</nav><div class="side-bottom"><div class="user-chip"><span class="avatar">{h(user["username"][:1].upper())}</span><div><b>{h(user["username"])}</b><small>{h(user["role_name"] if "role_name" in user.keys() else ROLES.get(user["role"],user["role"]))}</small></div></div><form method="post" action="/logout"><input type="hidden" name="csrf" value="{h(user["csrf"])}"><button class="logout">Выйти</button></form></div></aside>'
        date_label = datetime.now().strftime("%d.%m.%Y")
        header = f'<header class="topbar"><div><p class="eyebrow">КАТЕКС <span>·</span> УЧЁТ ИНСТРУМЕНТА</p><h1>{h(title)}</h1></div><div class="top-user"><span class="date-icon" aria-hidden="true">▦</span><span>{h(date_label)}</span></div></header>'
        shell = f'<div class="app-shell">{nav}<main class="main">{header}{f"<div class=notice>{h(notice)}</div>" if notice else ""}{body}</main></div>'
    else:
        shell = f'<main class="auth-wrap">{f"<div class=notice>{h(notice)}</div>" if notice else ""}{body}</main>'
    return f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f4f7fb"><title>{h(title)} · КАТЕКС</title><link rel="stylesheet" href="/static/style.css?v=20261020-2"></head><body>{shell}<script src="/static/app.js?v=20261020-2" defer></script></body></html>'


def field(label, name, value="", typ="text", attrs="", required=False):
    return f'<label class="field"><span>{h(label)}</span><input type="{typ}" name="{h(name)}" value="{h(value)}" {"required" if required else ""} {attrs}></label>'


def select_field(label, name, opts, selected=None, required=False, attrs=""):
    return f'<label class="field"><span>{h(label)}</span><select name="{h(name)}" {"required" if required else ""} {attrs}>{opts}</select></label>'


def machine_checkboxes(machines, selected=(), *, prefix="machine"):
    selected={str(x) for x in selected}
    if not machines:
        return '<p class="form-help">Сначала добавьте станок в справочник.</p>'
    return '<div class="machine-options"><label class="field machine-options-filter"><span>Найти станок</span><input type="search" placeholder="Название станка" autocomplete="off" data-machine-search></label><div class="machine-check-grid" tabindex="0" aria-label="Выбор станков">'+"".join(
        f'<label class="machine-check"><input type="checkbox" name="machine_ids" value="{m["id"]}" id="{h(prefix)}-{m["id"]}"{" checked" if str(m["id"]) in selected else ""}><span>{h(m["name"])}</span></label>'
        for m in machines
    )+'<p class="machine-no-results form-help" hidden>Станки не найдены</p></div></div>'


def csrf(user):
    return f'<input type="hidden" name="csrf" value="{h(user["csrf"])}"><input type="hidden" name="request_id" value="{secrets.token_hex(16)}">'


def trash_icon():
    return '<svg aria-hidden="true" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m3 0-.9 14H5.9L5 6m4 4v6m6-6v6"/></svg>'


def product_rows(con, products=None, user=None, archived=False):
    products=products if products is not None else con.execute("SELECT * FROM products WHERE active=1 ORDER BY item_class,category,name").fetchall()
    totals={p["id"]:usable_total(con,p["id"]) for p in products}
    buckets={}
    regrind_sources={str(json.loads(x["params_json"] or "{}").get("regrind_source_code") or "").casefold() for x in products}
    regrind_sources.discard("")
    regrind_designations={p["item_class"]+":"+p["category"].casefold()+":"+p["model"].strip().casefold() for p in products if json.loads(p["params_json"] or "{}").get("has_been_regrind") and p["model"].strip()}
    for p in products:
        params=json.loads(p["params_json"] or "{}")
        key=insert_base_code(params) if p["item_class"]=="consumable" and is_insert_category(p["category"]) else ""
        if not key and params.get("regrind_source_code"):
            key="regrind:"+str(params["regrind_source_code"]).casefold()
        if not key and p["code"].casefold() in regrind_sources:
            key="regrind:"+p["code"].casefold()
        designation=p["item_class"]+":"+p["category"].casefold()+":"+p["model"].strip().casefold()
        if not key and designation in regrind_designations:
            key="designation:"+designation
        if key: buckets.setdefault(key,[]).append(p)
    grouped={key:members for key,members in buckets.items() if len(members)>1 and (key.startswith("regrind:") or key.startswith("designation:") or any(json.loads(p["params_json"] or "{}").get("has_been_regrind") for p in members) or len({p["manufacturer"].strip().casefold() for p in members})>1)}
    def stock_cell(q,minimum,unit,breakdown=""):
        minimum=float(minimum or 0)
        ratio=(max(0,min(1,float(q)/minimum)) if minimum>0 else (1.0 if q>0 else 0.0))
        if q<0: return f'<div class="stock-error" role="alert"><b>{h(fmt_num(q))} {h(unit)}</b><small>Ошибка учёта — нужна сверка</small></div>'
        color=f"hsl({ratio*120:.0f} 68% 40%)"; width=max(8,round(ratio*100))
        return f'<div class="stock-visual"><b>{h(fmt_num(q))} {h(unit)}</b><small>мин. {h(fmt_num(minimum))}</small><div class="stock-meter"><i style="width:{width}%;background:{color}"></i></div>{breakdown}</div>'
    def row(p,extra_class=""):
        params=json.loads(p["params_json"] or "{}"); total=totals[p["id"]]
        image=f'<img class="thumb" src="/photo/{quote(p["photo"])}" alt="">' if p["photo"] else '<span class="thumb placeholder">К</span>'
        group=material_groups_text(params)
        item_name=product_primary_name(p)
        subtitle=product_secondary_name(p)
        name_cell=f'<b>{h(item_name)}</b><small>{h(p["code"])} · {h(subtitle)}</small>'
        if p["item_class"]=="consumable" and is_insert_category(p["category"]):
            insert_details=[f'Стружколом: {params["chipbreaker"]}' for _ in [0] if params.get("chipbreaker")]
            insert_details.extend(f'Материал: {params["insert_material"]}' for _ in [0] if params.get("insert_material"))
            if insert_details: name_cell+=f'<small>{h(" · ".join(insert_details))}</small>'
        mark='<span class="tag regrind-tag">После переточки</span>' if params.get("has_been_regrind") else ""
        if archived:
            action=(f'<form method="post" action="/product/restore" class="inline-action">{csrf(user)}<input type="hidden" name="id" value="{p["id"]}"><button class="text-link">Восстановить</button></form>' if user and has_permission(user,"catalog.edit") else "")
        else:
            action=f'<a class="text-link" href="/product/{p["id"]}">Открыть</a>'
            if user and has_permission(user,"catalog.edit"):
                action+=f'<form method="post" action="/product/archive" class="inline-action" onsubmit="return confirm(\'Переместить карточку в архив?\')">{csrf(user)}<input type="hidden" name="id" value="{p["id"]}"><button class="text-link danger-link">В архив</button></form>'
        return f'<tr class="{extra_class}"><td>{image}</td><td>{name_cell}</td><td><span class="tag">{"Инструмент" if p["item_class"]=="consumable" else "Оснастка"}</span>{mark}<small>{h(p["category"])}{(" · "+h(group)) if group else ""}</small></td><td>{h(params.get("diameter","—"))}</td><td>{h(params.get("l1","—"))}</td><td>{h(params.get("length","—"))}</td><td>{h(p["inventory_no"] or "—")}</td><td>{h(p["manufacturer"] or "—")}</td><td>{stock_cell(total,p["min_qty"],p["unit"],stock_breakdown(con,[p["id"]]))}</td><td>{h(p["location"] or "—")}</td><td>{action}</td></tr>'
    rows=[]; handled=set()
    for p in products:
        params=json.loads(p["params_json"] or "{}")
        key=insert_base_code(params) if p["item_class"]=="consumable" and is_insert_category(p["category"]) else ""
        if not key and params.get("regrind_source_code"): key="regrind:"+str(params["regrind_source_code"]).casefold()
        if not key and p["code"].casefold() in regrind_sources: key="regrind:"+p["code"].casefold()
        designation=p["item_class"]+":"+p["category"].casefold()+":"+p["model"].strip().casefold()
        if not key and designation in regrind_designations: key="designation:"+designation
        if key in grouped:
            if key in handled: continue
            handled.add(key); members=grouped[key]; group_id="product-group-"+str(len(rows))
            first=members[0]; category=first["category"]
            total=sum(totals[x["id"]] for x in members)
            minimum=sum(float(x["min_qty"] or 0) for x in members)
            amount=stock_cell(total,minimum,first["unit"],stock_breakdown(con,[x["id"] for x in members]))
            insert_group=is_insert_category(category)
            label=f'<b>{h(product_primary_name(first))}</b><small>{len(members)} карточки · общий остаток</small>'
            if insert_group:
                member_params=[json.loads(member["params_json"] or "{}") for member in members]
                for param_key,param_label in (("chipbreaker","Стружколом"),("insert_material","Материал")):
                    values=list(dict.fromkeys(str(item.get(param_key) or "").strip() for item in member_params))
                    values=[value for value in values if value]
                    if len(values)==1: label+=f'<small>{param_label}: {h(values[0])}</small>'
                    elif len(values)>1: label+=f'<small>{param_label}: различаются в карточках</small>'
            action=f'<button class="text-link show-all" type="button" data-target="{h(group_id)}">Показать все</button>'
            first_params=json.loads(first["params_json"] or "{}")
            tag="ISO" if first_params.get("insert_kind","iso")=="iso" else "П"
            caption=("ISO 1832" if tag=="ISO" else "Обозначение пластины") if insert_group else "Обозначение · разные состояния"
            rows.append(f'<tr class="plate-group"><td><span class="thumb placeholder">{tag}</span></td><td>{label}</td><td><span class="tag">Инструмент</span><small>{h(category)} · {caption}</small></td><td>—</td><td>—</td><td>—</td><td>—</td><td>несколько</td><td>{amount}</td><td>—</td><td>{action}</td></tr>')
            rows.extend(row(member,f'plate-detail {group_id}') for member in members)
        else: rows.append(row(p))
    return "".join(rows)


def products_select(con):
    rows=[]
    for p in con.execute("SELECT id,name,model,code FROM products WHERE active=1 ORDER BY name"):
        rows.append({"id":p["id"],"name":f'{p["model"] or p["name"]} · {p["code"]}'})
    return options(rows)


def products_in_state(con,state):
    rows=[]
    for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
        qty=balance(con,p["id"],state)
        if qty>0:
            rows.append({"id":p["id"],"name":f'{p["model"] or p["name"]} · {p["code"]} · {fmt_num(qty)} {p["unit"]}',"params":json.loads(p["params_json"] or "{}")})
    return rows


def worker_select(con, selected=None):
    rows=[]
    for r in con.execute("SELECT id,name FROM workers WHERE active=1 ORDER BY name"):
        names=[x[0] for x in con.execute("SELECT m.name FROM worker_machines wm JOIN machines m ON m.id=wm.machine_id WHERE wm.worker_id=? AND m.active=1 ORDER BY m.name",(r["id"],))]
        rows.append({"id":r["id"],"name":f'{r["name"]} · {", ".join(names) if names else "станок не назначен"}'})
    return options(rows,selected)


def machine_select(con, selected=None):
    return options([{"id":r["id"],"name":r["name"]} for r in con.execute("SELECT id,name FROM machines WHERE active=1 ORDER BY name")], selected)


def category_options(item_class, selected=""):
    items=ITEM_CATEGORIES.get(item_class, ITEM_CATEGORIES["consumable"])
    out=['<option value="">Выберите категорию</option>']
    out += [f'<option value="{h(v)}"{" selected" if v==selected else ""}>{h(v)}</option>' for v in items]
    out += [f'<option value="__custom__"{" selected" if selected and selected not in items else ""}>Своя категория…</option>']
    return "".join(out)


def tool_parameter_inputs(params,prefix=""):
    params=params if isinstance(params,dict) else {}
    decimals={"diameter","l1","length","shank","radius"}
    return '<div class="parameter-grid">'+"".join(field(label,prefix+key,params.get(key,""),typ="number" if key!="extra" else "text",attrs=('step="any" min="0"' if key in decimals else 'step="1" min="0"' if key!="extra" else 'placeholder="Например: покрытие, угол, исполнение"')) for key,label in TOOL_PARAMETER_FIELDS)+'</div>'


def category_parameter_fields(params,prefix="",item_class="consumable"):
    """Render type-specific parameters; data-family is switched by the card JS."""
    params=params if isinstance(params,dict) else {}
    def f(label,key,typ="text",attrs="",required=False): return field(label,prefix+key,params.get(key,""),typ=typ,attrs=attrs,required=required)
    def s(label,key,choices,required=False):
        opts=options([{"id":v,"name":caption} for v,caption in choices],params.get(key),"Выберите")
        return select_field(label,prefix+key,opts,params.get(key),required)
    if item_class=="fixture":
        params=dict(params)
        interface=str(params.get("interface") or "")
        params.setdefault("fixture_group","turning" if interface in {"BMT","BOT","VDI"} else "milling" if interface in {"BT","BBT","SK","HSK","Morse"} or params.get("fixture_type") in {"holder","chuck","collet"} else "general")
        params.setdefault("fixture_reach",params.get("reach",""))
        params.setdefault("fixture_length",params.get("length",""))
        old=params.get("fixture_type")
        if old in {"holder","chuck","collet"}:
            params["fixture_type"]=("mill_" if params["fixture_group"]=="milling" else "turn_")+old
        elif old in {"vise","boring","other"}: params["fixture_type"]=old
        if not params.get("fixture_type") and params["fixture_group"]=="turning": params["fixture_type"]="turn_holder"
        if not params.get("fixture_type") and params["fixture_group"]=="milling": params["fixture_type"]="mill_holder"
        fixture_types=[("bmt_block","Блок BMT"),("bot_block","Блок BOT"),("vdi_block","Блок VDI"),("turn_holder","Токарный держатель"),("transition_bushing","Переходная втулка"),("turn_collet","Токарная цанга"),("turn_chuck","Токарный патрон"),("mill_holder","Фрезерная оправка / держатель"),("mill_chuck","Фрезерный патрон"),("mill_collet","Фрезерная цанга"),("vise","Тиски / зажим"),("boring","Расточная система"),("angle_head","Угловая головка"),("other","Другое")]
        return f'<div class="parameter-family" data-family="fixture"><h3>Параметры оснастки</h3><div class="parameter-grid">{s("Группа оснастки","fixture_group",[("turning","Токарная"),("milling","Фрезерная"),("general","Общая")],True)}{s("Подтип оснастки","fixture_type",fixture_types,True)}{s("Интерфейс станка / шпинделя","interface",[(x,x) for x in ("BMT","BOT","VDI","BT","BBT","SK","HSK","Morse","Цилиндрический","Прочее")])}{f("Размер интерфейса / конуса","interface_size")}{s("Исполнение","orientation",[("axial","Осевое"),("radial","Радиальное"),("straight","Прямое"),("angle","Угловое")])}</div><div data-fixture-detail="block"><h4>Габариты токарного блока</h4><div class="parameter-grid">'+''.join(f("Размер "+k.upper()+", мм","block_"+k,"number",'step="1" min="0"') for k in "abcdefgh")+'</div></div><div data-fixture-detail="holder"><h4>Габариты держателя</h4><div class="parameter-grid">'+''.join(f(label,key,"number",'step="1" min="0"') for label,key in (("D, мм","holder_d"),("H, мм","holder_h"),("H1, мм","holder_h1"),("L, мм","holder_l")))+'</div></div><div data-fixture-detail="bushing"><h4>Переходная втулка</h4><div class="parameter-grid">'+f("Наружный конус / интерфейс","bushing_outer")+f("Внутренний конус / интерфейс","bushing_inner")+f("Длина втулки, мм","fixture_length","number",'step="1" min="0"')+'</div></div><div data-fixture-detail="vise"><h4>Тиски и зажимы</h4><div class="parameter-grid">'+f("Ширина губок, мм","jaw_width","number",'step="1" min="0"')+f("Максимальное раскрытие, мм","vise_opening","number",'step="1" min="0"')+f("Высота губок, мм","jaw_height","number",'step="1" min="0"')+'</div></div><div data-fixture-detail="boring"><h4>Расточная система</h4><div class="parameter-grid">'+f("Минимальный диаметр растачивания, мм","bore_min","number",'step="1" min="0"')+f("Максимальный диаметр растачивания, мм","bore_max","number",'step="1" min="0"')+f("Точность / биение, мм","accuracy","number",'step="any" min="0"')+f("Состав набора","set_components")+'</div></div><div data-fixture-detail="mill"><h4>Фрезерная оснастка</h4><div class="parameter-grid">'+f("Диаметр зажима, мм","clamp_diameter","number",'step="1" min="0"')+f("Рабочий вылет, мм","fixture_reach","number",'step="1" min="0"')+f("Длина, мм","fixture_length","number",'step="1" min="0"')+f("Максимальные обороты, об/мин","max_rpm","number",'step="1" min="0"')+'</div></div><div data-fixture-detail="common"><div class="parameter-grid">'+f("Диапазон зажима / отверстия","range")+f("Другие параметры","fixture_extra")+'</div></div></div>'
    geometry='<div class="parameter-grid">'+"".join(field(label,prefix+key,params.get(key,""),typ="number" if key!="extra" else "text",attrs=('step="any" min="0"' if key in {"diameter","l1","length","shank","radius"} else 'step="1" min="0"' if key in {"z","radius"} else 'placeholder="Например: покрытие, угол, исполнение"')) for key,label in TOOL_PARAMETER_FIELDS)+'</div>'
    insert='<div class="parameter-grid">'+f("Обозначение устанавливаемой сменной пластины","insert_designation",attrs='placeholder="Например: APKT1604PDR"')+f("Количество гнезд / пластин","insert_count","number",'step="1" min="0"')+'</div>'
    thread='<div class="parameter-grid">'+s("Тип резьбонарезного инструмента","thread_tool_type",[("single","Однопрофильный"),("multi","Многопрофильный"),("tap_cut","Метчик режущий"),("tap_form","Метчик раскатной"),("die","Плашка"),("other","Другое")],True)+f("Под какую резьбу","thread_size",attrs='placeholder="Например: M16, M16x1.5, G1/2"',required=True)+f("Шаг резьбы, мм","pitch","number",'step="any" min="0"',required=True)+s("Профиль / стандарт резьбы","thread_standard",[(x,x) for x in ("Метрическая ISO","UN / UNC / UNF","Трубная BSP / G","NPT","Трапецеидальная","Другая")])+s("Направление резьбы","thread_hand",[("R","Правая"),("L","Левая")])+s("Обработка отверстия","hole_type",[("through","Сквозное"),("blind","Глухое"),("any","Универсальное")])+'</div>'
    coolant='<div class="parameter-grid">'+s("Подача СОЖ","coolant",[("none","Не предусмотрена"),("external","Внешняя"),("internal","Внутренняя через инструмент"),("both","Внешняя и внутренняя")],True)+'</div>'
    plate='<div class="parameter-grid">'+s("Тип пластины","insert_kind",[("iso","Токарная пластина по ISO 1832"),("grooving","Канавочная пластина"),("parting","Отрезная пластина"),("thread","Резьбовая пластина")],True)+'<div data-insert-kind="thread">'+f("Обозначение резьбовой пластины","thread_insert_designation",attrs='placeholder="Например: 16ERM AG60, 16IRAG60-TC, 16IR1.50 ISO, 21N3.5ISO"',required=True)+f("Шаг резьбы, мм","thread_insert_pitch","number",'step="any" min="0"')+'</div><div data-insert-kind="special">'+f("Обозначение канавочной / отрезной пластины","plate_designation",attrs='placeholder="Например: MGMN300-M или GTN-2"',required=True)+'</div>'+f("Тип стружколома","chipbreaker",attrs='placeholder="Например: -M, -GM, -GF"')+f("Материал пластины","insert_material",attrs='placeholder="Например: твердый сплав, кермет, керамика"')+'</div>'
    html='<div class="parameter-family" data-family="geometry"><h3>Размеры и параметры инструмента</h3>'+geometry+'</div>'
    html+='<div class="parameter-family" data-family="insert"><h3>Сменная пластина для корпусного инструмента</h3>'+insert+'</div>'
    html+='<div class="parameter-family" data-family="thread"><h3>Параметры резьбы</h3>'+thread+'</div>'
    html+='<div class="parameter-family" data-family="coolant"><h3>Подача СОЖ</h3>'+coolant+'</div>'
    html+='<div class="parameter-family" data-family="plate">'+plate+'</div>'
    return html


def normalized_category_params(data,prefix,category,item_class):
    category=str(category or "").strip().casefold()
    params=normalized_tool_params(data,prefix) if item_class=="consumable" and category not in {"пластина","пластины","пластина токарная","токарная пластина","резьбовая пластина"} else {}
    def val(key): return str(data.get(prefix+key,"")).strip()
    if item_class=="fixture":
        for key,form_key in (("fixture_group","fixture_group"),("fixture_type","fixture_type"),("interface","interface"),("interface_size","interface_size"),("orientation","orientation"),("range","range"),("reach","fixture_reach"),("length","fixture_length"),("jaw_width","jaw_width"),("accuracy","accuracy"),("extra","fixture_extra"),("bushing_outer","bushing_outer"),("bushing_inner","bushing_inner"),("vise_opening","vise_opening"),("jaw_height","jaw_height"),("bore_min","bore_min"),("bore_max","bore_max"),("set_components","set_components"),("clamp_diameter","clamp_diameter"),("max_rpm","max_rpm")):
            x=val(form_key)
            if not x: continue
            if key in {"reach","length","jaw_width","accuracy","vise_opening","jaw_height","bore_min","bore_max","clamp_diameter","max_rpm"}:
                try: n=float(x)
                except ValueError: raise ValueError(f"Параметр {key} должен быть числом.")
                if not math.isfinite(n) or n<0: raise ValueError(f"Параметр {key} должен быть неотрицательным числом.")
                if key!="accuracy" and not n.is_integer(): raise ValueError(f"Параметр {key} должен быть целым числом.")
                x=fmt_num(n)
            params[key]=x
        for k in "abcdefgh":
            x=val("block_"+k)
            if x:
                try: n=float(x)
                except ValueError: raise ValueError(f"Размер {k.upper()} блока должен быть числом.")
                if n<0 or not n.is_integer(): raise ValueError(f"Размер {k.upper()} блока должен быть целым неотрицательным числом.")
                params["block_"+k]=str(int(n))
        for k in ("d","h","h1","l"):
            x=val("holder_"+k)
            if x:
                try: n=float(x)
                except ValueError: raise ValueError(f"Размер {k.upper()} держателя должен быть числом.")
                if n<0 or not n.is_integer(): raise ValueError(f"Размер {k.upper()} держателя должен быть целым неотрицательным числом.")
                params["holder_"+k]=str(int(n))
        if params.get("bore_min") and params.get("bore_max") and float(params["bore_max"])<float(params["bore_min"]):
            raise ValueError("Максимальный диаметр растачивания должен быть не меньше минимального.")
        if params.get("fixture_group") not in {"turning","milling","general"}: raise ValueError("Выберите группу оснастки: токарная, фрезерная или общая.")
        allowed={"turning":{"bmt_block","bot_block","vdi_block","turn_holder","transition_bushing","turn_collet","turn_chuck"},"milling":{"mill_holder","mill_chuck","mill_collet","vise","boring","angle_head"},"general":{"transition_bushing","vise","boring","other"}}
        if params.get("fixture_type") not in allowed[params["fixture_group"]]: raise ValueError("Выберите подтип оснастки, соответствующий ее группе.")
        return params
    if category in {"пластина","пластины","пластина токарная","токарная пластина","резьбовая пластина"}:
        kind=val("insert_kind") or ("thread" if category=="резьбовая пластина" else "iso")
        if kind not in {"iso","thread","grooving","parting"}: raise ValueError("Выберите исполнение пластины.")
        params["insert_kind"]=kind
        if kind=="thread":
            designation=val("thread_insert_designation")
            if not designation: raise ValueError("Укажите обозначение резьбовой пластины.")
            params["thread_insert_designation"]=designation
            pitch=val("thread_insert_pitch")
            if pitch:
                try: n=float(pitch)
                except ValueError: raise ValueError("Шаг резьбы должен быть числом.")
                if not math.isfinite(n) or n<=0: raise ValueError("Шаг резьбы должен быть больше нуля.")
                params["pitch"]=fmt_num(n)
        elif kind in {"grooving","parting"}:
            designation=val("plate_designation")
            if not designation: raise ValueError("Укажите заводское обозначение канавочной или отрезной пластины.")
            params["plate_designation"]=designation
        else: params["insert_iso"]=parse_iso_insert(data,prefix)
        for key in ("chipbreaker","insert_material"):
            if val(key): params[key]=val(key)
        return params
    if category in THREAD_TOOL_CATEGORIES:
        params["thread_tool_type"]=val("thread_tool_type")
        params["thread_size"]=val("thread_size")
        params["thread_standard"]=val("thread_standard")
        params["thread_hand"]=val("thread_hand")
        params["hole_type"]=val("hole_type")
        if not params["thread_size"]: raise ValueError("Укажите размер обрабатываемой резьбы.")
        pitch=val("pitch")
        if not params["thread_tool_type"]: raise ValueError("Выберите тип резьбонарезного инструмента.")
        if params["thread_tool_type"] not in {"single","multi","tap_cut","tap_form","die","other"}: raise ValueError("Выберите корректный тип резьбонарезного инструмента.")
        if not pitch: raise ValueError("Укажите шаг резьбы.")
        try: n=float(pitch)
        except ValueError: raise ValueError("Шаг резьбы должен быть числом.")
        if not math.isfinite(n) or n<=0: raise ValueError("Шаг резьбы должен быть больше нуля.")
        params["pitch"]=fmt_num(n)
    if category in AXIAL_TOOL_CATEGORIES:
        params["coolant"]=val("coolant")
        if not params["coolant"]: raise ValueError("Укажите способ подачи СОЖ или выберите «Не предусмотрена».")
        if params["coolant"] not in {"none","external","internal","both"}: raise ValueError("Выберите корректный способ подачи СОЖ.")
    if category in BODY_TOOL_CATEGORIES:
        params["insert_designation"]=val("insert_designation")
        count=val("insert_count")
        if count:
            try: n=float(count)
            except ValueError: raise ValueError("Количество гнезд для пластин должно быть целым числом.")
            if n<0 or not n.is_integer(): raise ValueError("Количество гнезд для пластин должно быть целым числом.")
            params["insert_count"]=str(int(n))
    return {k:v for k,v in params.items() if v not in ("",None)}


def normalized_tool_params(data,prefix=""):
    params={key:str(data.get(prefix+key,"")).strip() for key,_ in TOOL_PARAMETER_FIELDS}
    for key in ("diameter","l1","length","shank","z","radius"):
        if not params.get(key): continue
        try: number=float(params[key].replace(",", "."))
        except ValueError: raise ValueError(f"Параметр {key} должен быть числом.")
        if not math.isfinite(number) or number<0: raise ValueError(f"Параметр {key} должен быть неотрицательным числом.")
        if key=="z" and not number.is_integer(): raise ValueError(f"Параметр {key} должен быть целым числом.")
        params[key]=fmt_num(number)
    return {key:value for key,value in params.items() if value}


def iso_insert_fields(prefix="", iso=None):
    iso=iso or {}
    out=[]
    for key,label,values in ISO_1832_FIELDS:
        out.append(select_field(label,prefix+"iso_"+key,options([{"id":v,"name":caption} for caption,v in values],iso.get(key),"Выберите"),iso.get(key),True))
    out += [field("Размер пластины — позиция 5, например 12",prefix+"iso_size",iso.get("size",""),attrs='maxlength="2" pattern="[A-Za-z0-9]{2}" placeholder="12"',required=True),
             field("Толщина — позиция 6, код ISO, например 04",prefix+"iso_thickness",iso.get("thickness",""),attrs='maxlength="2" pattern="[A-Za-z0-9]{2}" placeholder="04"',required=True),
             field("Радиус вершины — позиция 7, например 02",prefix+"iso_corner",iso.get("corner",""),attrs='maxlength="2" pattern="[0-9]{2}" placeholder="02"',required=True)]
    return '<div class="iso-grid">'+"".join(out)+'</div><p class="form-help">Семь полей формируют обязательную часть обозначения ISO 1832. Пример: C · C · G · X · 12 · 04 · 02 → CCGX120402.</p><p class="iso-preview">Обозначение: <b id="'+h(prefix)+'iso-preview">—</b></p>'


def insert_base_code(params):
    iso=params.get("insert_iso",{}) if isinstance(params,dict) else {}
    if isinstance(params,dict):
        kind=params.get("insert_kind")
        if kind=="thread": return str(params.get("thread_insert_designation") or "")
        if kind in {"grooving","parting"}: return str(params.get("plate_designation") or "")
    keys=["shape","clearance","tolerance","fixing","size","thickness","corner"]
    if not all(str(iso.get(k,"")) for k in keys): return ""
    return "".join(str(iso[k]).upper() for k in keys)


def is_insert_category(category):
    return str(category or "").strip().casefold() in {"пластина","пластины","пластина токарная","токарная пластина","резьбовая пластина"}


def parse_iso_insert(data,prefix=""):
    iso={k:data.get(prefix+"iso_"+k,"").strip().upper() for k in ("shape","clearance","tolerance","fixing","size","thickness","corner")}
    if not all(iso.values()): raise ValueError("Для пластины заполните все 7 полей обозначения ISO 1832.")
    iso["size"]=iso["size"].zfill(2); iso["thickness"]=iso["thickness"].zfill(2); iso["corner"]=iso["corner"].zfill(2)
    if len(iso["shape"])!=1 or len(iso["clearance"])!=1 or len(iso["tolerance"])!=1 or len(iso["fixing"])!=1 or any(len(iso[k])!=2 for k in ("size","thickness","corner")):
        raise ValueError("Проверьте поля ISO 1832: четыре буквы, код размера, толщины и радиуса.")
    allowed={"shape":{v for _,v in ISO_1832_FIELDS[0][2]},"clearance":{v for _,v in ISO_1832_FIELDS[1][2]},"tolerance":{v for _,v in ISO_1832_FIELDS[2][2]},"fixing":{v for _,v in ISO_1832_FIELDS[3][2]}}
    if any(iso[k] not in allowed[k] for k in allowed) or not re.fullmatch(r"[A-Z0-9]{2}",iso["size"]) or not re.fullmatch(r"[A-Z0-9]{2}",iso["thickness"]) or not re.fullmatch(r"\d{2}",iso["corner"]):
        raise ValueError("Одно или несколько полей не соответствуют формату обозначения ISO 1832.")
    return iso


def material_group_field(name="material_groups",selected=None):
    if isinstance(selected,str): selected=[selected] if selected else []
    selected=set(selected or [])
    checks="".join(f'<label class="iso513-option"><input type="checkbox" name="{h(name)}" value="{k}"{" checked" if k in selected else ""}><span><b>{k}</b> — {h(v)}</span></label>' for k,v in ISO_513_GROUPS.items())
    return f'<fieldset class="field iso513-field"><legend>Группы применяемости по ISO 513 (обрабатываемый материал)</legend><div class="iso513-options">{checks}</div><small class="form-help">Можно выбрать несколько групп.</small></fieldset>'


def material_groups_text(params):
    groups=params.get("material_groups") or ([params.get("material_group")] if params.get("material_group") else [])
    return ", ".join(f'{g} — {ISO_513_GROUPS[g]}' for g in groups if g in ISO_513_GROUPS)


def category_parameter_summary(params):
    labels={"diameter":"Ø, мм","l1":"l1, мм","length":"L, мм","z":"z — число зубьев","shank":"d хвостовика, мм","radius":"r, мм","insert_designation":"Обозначение сменной пластины","insert_count":"Гнезда для пластин","thread_insert_designation":"Резьбовая пластина","plate_designation":"Канавочная / отрезная пластина","chipbreaker":"Тип стружколома","insert_material":"Материал пластины","pitch":"Шаг резьбы, мм","thread_size":"Размер резьбы","thread_standard":"Стандарт резьбы","thread_hand":"Направление резьбы","hole_type":"Исполнение отверстия","coolant":"Подача СОЖ","fixture_group":"Группа оснастки","fixture_type":"Подтип оснастки","interface":"Интерфейс","interface_size":"Размер интерфейса","orientation":"Исполнение","range":"Диапазон зажима / отверстия","reach":"Рабочий вылет, мм","jaw_width":"Ширина губок / корпуса, мм","accuracy":"Точность / биение, мм","bore_min":"Мин. диаметр растачивания, мм","bore_max":"Макс. диаметр растачивания, мм","vise_opening":"Максимальное раскрытие, мм","jaw_height":"Высота губок, мм","clamp_diameter":"Диаметр зажима, мм","max_rpm":"Максимальные обороты, об/мин","bushing_outer":"Наружный конус","bushing_inner":"Внутренний конус","set_components":"Состав набора","extra":"Другие параметры"}
    labels.update({"block_"+k:"Размер "+k.upper()+", мм" for k in "abcdefgh"})
    labels.update({"holder_d":"Размер D держателя, мм","holder_h":"Размер H держателя, мм","holder_h1":"Размер H1 держателя, мм","holder_l":"Размер L держателя, мм"})
    pretty={"R":"Правая","L":"Левая","axial":"Осевое","radial":"Радиальное","straight":"Прямое","angle":"Угловое","none":"Не предусмотрена","external":"Внешняя","internal":"Внутренняя через инструмент","both":"Внешняя и внутренняя","turning":"Токарная","milling":"Фрезерная","general":"Общая","bmt_block":"Блок BMT","bot_block":"Блок BOT","vdi_block":"Блок VDI","turn_holder":"Токарный держатель","transition_bushing":"Переходная втулка","turn_collet":"Токарная цанга","turn_chuck":"Токарный патрон","mill_holder":"Фрезерная оправка / держатель","mill_chuck":"Фрезерный патрон","mill_collet":"Фрезерная цанга","vise":"Тиски / зажим","boring":"Расточная система","angle_head":"Угловая головка","other":"Другое"}
    rows=[]
    for key,label in labels.items():
        value=params.get(key)
        if value: rows.append(f'<div><small>{h(label)}</small><b>{h(pretty.get(str(value),value))}</b></div>')
    if params.get("insert_iso"):
        rows.append(f'<div><small>Обозначение ISO 1832</small><b>{h(insert_base_code(params))}</b></div>')
    if params.get("material_groups") or params.get("material_group"):
        rows.append(f'<div><small>Группы ISO 513</small><b>{h(material_groups_text(params))}</b></div>')
    return "".join(rows) or '<div><small>Параметры</small><b>—</b></div>'


class App(BaseHTTPRequestHandler):
    server_version = "KATEX/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send(self, body, status=200, content_type="text/html; charset=utf-8", headers=None):
        if getattr(self, "_buffer_response", False):
            self._pending_response = (body, status, content_type, headers)
            return
        data=body.encode("utf-8") if isinstance(body,str) else body
        self.send_response(status); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(data)))
        self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","same-origin")
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)

    def redirect(self, path, notice="", headers=None):
        if notice:
            base,sep,fragment=path.partition("#")
            path=base+("&" if "?" in base else "?")+"msg="+quote(notice)+(sep+fragment if sep else "")
        self.send("", 303, headers={"Location": path, **(headers or {})})

    def body_parts(self):
        length=int(self.headers.get("Content-Length","0"))
        if length > MAX_BODY: raise ValueError("Файл слишком большой. Максимальный размер запроса — 8 МБ.")
        body=self.rfile.read(length)
        ctype=self.headers.get("Content-Type","")
        if ctype.startswith("multipart/form-data"):
            message=BytesParser(policy=default).parsebytes(b"Content-Type: "+ctype.encode()+b"\r\nMIME-Version: 1.0\r\n\r\n"+body)
            data={}; files={}
            for part in message.iter_parts():
                name=part.get_param("name",header="content-disposition")
                if not name: continue
                payload=part.get_payload(decode=True) or b""
                filename=part.get_filename()
                if filename: files[name]=(filename,payload,part.get_content_type())
                else:
                    value=payload.decode(part.get_content_charset() or "utf-8",errors="replace")
                    if name in ("permissions","machine_ids","material_groups","new_material_groups"): data.setdefault(name,[]).append(value)
                    else: data[name]=value
            return data,files
        parsed=parse_qs(body.decode("utf-8",errors="replace"),keep_blank_values=True)
        return {k:(v if k in ("permissions","machine_ids","material_groups","new_material_groups") else v[-1]) for k,v in parsed.items()},{}

    def current_user(self, con):
        raw=self.headers.get("Cookie","")
        token=""
        for item in raw.split(";"):
            k,_,v=item.strip().partition("=")
            if k=="ts_session": token=v
        if not token: return None
        row=con.execute("SELECT s.csrf,s.expires_at,u.*,COALESCE(rd.name,u.role) AS role_name,COALESCE(rd.permissions_json,'[]') AS permissions_json FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN role_definitions rd ON rd.code=u.role WHERE s.token=? AND u.active=1",(token,)).fetchone()
        if not row or row["expires_at"] < now_iso():
            con.execute("DELETE FROM sessions WHERE token=?",(token,)); return None
        return row

    def require(self, user, roles=None):
        if not user: raise PermissionError("Для продолжения войдите в систему.")
        if isinstance(roles,str):
            if not has_permission(user,roles): raise PermissionError("Недостаточно прав для этой операции.")
        elif roles and user["role"] not in roles: raise PermissionError("Недостаточно прав для этой операции.")

    def landing_path(self,user):
        for permission,path in (("dashboard","/dashboard"),("transactions.manage","/transactions"),("catalog.view","/catalog"),("reports.view","/reports"),("purchasing.view","/purchasing"),("history.view","/history")):
            if has_permission(user,permission): return path
        return "/login"

    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; query=parse_qs(parsed.query); notice=query.get("msg",[""])[0]
        if path=="/static/app.js":
            return self.send((ROOT/"app.js").read_bytes(),content_type="application/javascript; charset=utf-8",headers={"Cache-Control":"no-cache"})
        if path=="/static/style.css":
            try: return self.send((ROOT/"style.css").read_bytes(),content_type="text/css; charset=utf-8",headers={"Cache-Control":"no-cache"})
            except FileNotFoundError: return self.send("",404)
        if path=="/static/logo.png":
            try: return self.send((ROOT/"logo.png").read_bytes(),content_type="image/png",headers={"Cache-Control":"public, max-age=86400"})
            except FileNotFoundError: return self.send("",404)
        with db() as con:
            user=self.current_user(con)
            try:
                if path=="/photo/"+Path(path).name and path.startswith("/photo/"):
                    self.require(user)
                    name=Path(path).name
                    if not re.fullmatch(r"[a-f0-9]{32}\.[a-zA-Z0-9]{1,8}",name): return self.send("Не найдено",404)
                    f=UPLOAD_DIR/name
                    if not f.exists(): return self.send("Не найдено",404)
                    return self.send(f.read_bytes(),content_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream",headers={"Cache-Control":"private, max-age=3600"})
                if path=="/" or path=="/dashboard":
                    if not user: return self.auth_page(con,notice)
                    if not has_permission(user,"dashboard"): return self.redirect(self.landing_path(user))
                    return self.dashboard(con,user,notice)
                if path=="/login": return self.auth_page(con,notice)
                if path=="/setup": return self.setup_page(con,notice)
                self.require(user)
                if path=="/api/held": return self.held_api(con,user,query)
                if path=="/api/products": return self.products_api(con,user,query)
                if path=="/catalog": self.require(user,"catalog.view"); return self.catalog(con,user,notice)
                if path.startswith("/product/"):
                    slug=path.rsplit("/",1)[1]; pid=0 if slug=="new" else int(slug)
                    self.require(user,"catalog.view")
                    return self.product_page(con,user,pid,notice)
                if path=="/transactions": self.require(user,"transactions.manage"); return self.transactions(con,user,notice)
                if path=="/regrind/export": self.require(user,"transactions.manage"); return self.export_regrind_queue(con,user)
                if path=="/purchasing":
                    self.require(user,"purchasing.view")
                    if query.get("export")==["1"]: return self.export_purchase_request(con,user,query)
                    return self.purchasing(con,user,notice)
                if path=="/reports":
                    self.require(user,"reports.view")
                    if query.get("export")==["balances"]: return self.export_balances(con,user,query)
                    if query.get("export")==["history"]: return self.export_history(con,user,query)
                    return self.reports(con,user,query,notice)
                if path=="/history":
                    if not (has_permission(user,"history.view") or has_permission(user,"transactions.manage")): raise PermissionError("Недостаточно прав для этой операции.")
                    return self.history(con,user,query,notice)
                if path=="/admin": return self.admin(con,user,notice)
                return self.page_error(user,404,"Страница не найдена")
            except PermissionError as e: return self.page_error(user,403,str(e))
            except (ValueError,KeyError) as e: return self.page_error(user,404,"Запрошенная запись не найдена" if isinstance(e,KeyError) else "Некорректный параметр")

    def do_POST(self):
        user=None
        self._buffer_response=True
        try:
            data,files=self.body_parts(); path=urlparse(self.path).path
            with db() as con:
                begin_write(con)
                if path=="/setup": self.setup_admin(con,data)
                elif path=="/login": self.login(con,data)
                else:
                    user=self.current_user(con)
                    self.require(user)
                    if not hmac.compare_digest(data.get("csrf",""),user["csrf"]):
                        raise PermissionError("Проверка формы не пройдена. Обновите страницу.")
                    if path=="/logout":
                        con.execute("DELETE FROM sessions WHERE user_id=?",(user["id"],))
                        self.redirect("/login","Вы вышли из системы")
                    else:
                        request_id=data.get("request_id", "")
                        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                            raise ValueError("Форма устарела. Обновите страницу перед проведением операции.")
                        payload={k:v for k,v in data.items() if k not in ("csrf","request_id")}
                        fingerprint=hashlib.sha256((path+json.dumps(payload,sort_keys=True,ensure_ascii=False)+json.dumps({k:hashlib.sha256(v[1]).hexdigest() for k,v in files.items()},sort_keys=True)).encode()).hexdigest()
                        previous=con.execute("SELECT fingerprint,location FROM submitted_forms WHERE user_id=? AND request_id=?",(user["id"],request_id)).fetchone()
                        if previous:
                            if previous["fingerprint"]!=fingerprint:
                                raise ValueError("Эта форма уже проведена с другими данными. Обновите страницу.")
                            self.redirect(previous["location"])
                        else:
                            self.dispatch_post(con,user,path,data,files)
                            response=self._pending_response
                            if response[1]==303:
                                con.execute("INSERT INTO submitted_forms VALUES(?,?,?,?,?)",(user["id"],request_id,fingerprint,response[3]["Location"],now_iso()))
            # No response is sent until the transaction commits successfully.
        except PermissionError as e:
            self.page_error(user,403,str(e))
        except (ValueError, sqlite3.IntegrityError) as e:
            message=str(e) if isinstance(e,ValueError) else "Не удалось сохранить запись: проверьте уникальность кода, логина и назначения пользователя."
            self.page_error(user,400,message)
        except sqlite3.OperationalError:
            self.page_error(user,503,"База занята или недоступна. Повторите отправку формы; повторная операция не будет создана.")
        self._buffer_response=False
        self.send(*self._pending_response)

    def dispatch_post(self,con,user,path,data,files):
        if path=="/product/save": return self.save_product(con,user,data,files)
        if path=="/product/archive": return self.archive_product(con,user,data)
        if path=="/product/restore": return self.restore_product(con,user,data)
        if path=="/transaction/receipt": return self.receipt(con,user,data,files)
        if path=="/transaction/issue": return self.issue(con,user,data)
        if path=="/transaction/settle": return self.settle(con,user,data)
        if path=="/transaction/writeoff": return self.writeoff(con,user,data)
        if path=="/transaction/regrind/send": return self.send_regrind(con,user,data)
        if path=="/transaction/regrind/send-all": return self.send_all_regrind(con,user,data)
        if path=="/transaction/regrind/receive": return self.receive_regrind(con,user,data)
        if path=="/movement/edit": return self.edit_movement(con,user,data)
        if path=="/movement/cancel": return self.cancel_movement(con,user,data)
        if path=="/purchasing/create": return self.create_purchase_request(con,user,data)
        if path=="/purchasing/close": return self.change_purchase_request(con,user,data,"close")
        if path=="/purchasing/archive": return self.change_purchase_request(con,user,data,"archive")
        if path=="/purchasing/restore": return self.change_purchase_request(con,user,data,"restore")
        if path=="/admin/machine": return self.add_machine(con,user,data)
        if path=="/admin/machine/edit": return self.edit_machine(con,user,data)
        if path=="/admin/worker": return self.add_worker(con,user,data)
        if path=="/admin/worker/edit": return self.edit_worker(con,user,data)
        if path=="/admin/user": return self.add_user(con,user,data)
        if path=="/admin/role": return self.add_role(con,user,data)
        if path=="/admin/role/edit": return self.edit_role(con,user,data)
        if path=="/admin/role/delete": return self.delete_role(con,user,data)
        if path=="/admin/user/delete": return self.delete_user(con,user,data)
        if path=="/admin/user/restore": return self.restore_user(con,user,data)
        if path=="/admin/machine/delete": return self.delete_machine(con,user,data)
        if path=="/admin/machine/restore": return self.restore_machine(con,user,data)
        if path=="/admin/worker/delete": return self.delete_worker(con,user,data)
        if path=="/admin/worker/restore": return self.restore_worker(con,user,data)
        return self.page_error(user,404,"Страница не найдена")
    def auth_page(self,con,notice=""):
        count=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count==0: return self.setup_page(con,notice)
        body=f'''<section class="auth-card"><div class="auth-brand"><img src="/static/logo.png" alt="КАТЕКС — учет инструмента" width="168" height="168"></div><h1>Вход в систему</h1><p>Войдите под своей учетной записью.</p><form method="post" action="/login">{field("Логин","username",required=True)}{field("Пароль","password",typ="password",required=True)}<button class="button primary full">Войти</button></form></section>'''
        return self.send(layout("Вход",body,notice=notice))

    def setup_page(self,con,notice=""):
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0]: return self.auth_page(con,notice or "Первичная настройка уже выполнена")
        body=f'''<section class="auth-card"><div class="auth-brand"><img src="/static/logo.png" alt="КАТЕКС — учет инструмента" width="168" height="168"></div><span class="tag">Первый запуск</span><h1>Создайте администратора</h1><p>Задайте учетную запись владельца системы. Пароль должен содержать не менее 10 символов.</p><form method="post" action="/setup">{field("Логин администратора","username",required=True)}{field("Пароль","password",typ="password",attrs="minlength=10",required=True)}{field("Повторите пароль","password2",typ="password",attrs="minlength=10",required=True)}<button class="button primary full">Создать учетную запись</button></form></section>'''
        return self.send(layout("Первый запуск",body,notice=notice))

    def setup_admin(self,con,data):
        begin_write(con)
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0]: return self.redirect("/login","Первичная настройка уже выполнена")
        username=data.get("username","").strip(); password=data.get("password","")
        if len(username)<3 or len(password)<10 or password!=data.get("password2"): raise ValueError("Проверьте логин, пароль и подтверждение пароля.")
        con.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",(username,pw_hash(password),"ADMIN",now_iso()))
        return self.redirect("/login","Администратор создан. Войдите в систему.")

    def login(self,con,data):
        row=con.execute("SELECT * FROM users WHERE username=? AND active=1",(data.get("username","").strip(),)).fetchone()
        if not row or not pw_check(data.get("password",""),row["password_hash"]): return self.redirect("/login","Неверный логин или пароль")
        token=secrets.token_urlsafe(32); csrf_token=secrets.token_urlsafe(24)
        exp=(datetime.now(timezone.utc)+timedelta(hours=12)).replace(microsecond=0).isoformat()
        con.execute("INSERT INTO sessions(token,user_id,csrf,expires_at) VALUES(?,?,?,?)",(token,row["id"],csrf_token,exp))
        permission_row=con.execute("SELECT permissions_json FROM role_definitions WHERE code=?",(row["role"],)).fetchone()
        landing=self.landing_path({"role":row["role"],"permissions_json":permission_row[0] if permission_row else "[]"})
        self.redirect(landing,"Добро пожаловать",headers={"Set-Cookie":f"ts_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})

    def dashboard(self,con,user,notice=""):
        if user["role"]=="WORKER":
            if not user["worker_id"]: return self.send(layout("Главная",'<div class="panel">Учетная запись не связана с карточкой рабочего. Обратитесь к администратору.</div>',user,notice,active="dashboard"))
            w=con.execute("SELECT * FROM workers WHERE id=?",(user["worker_id"],)).fetchone()
            held=[]
            for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
                for state,label in STATES.items():
                    machine_ids=[r[0] for r in con.execute("SELECT DISTINCT machine_id FROM movements WHERE product_id=? AND worker_id=? AND machine_id IS NOT NULL AND voided_at IS NULL",(p["id"],w["id"]))]
                    for mid in machine_ids:
                        q=balance(con,p["id"],state,worker_id=w["id"],machine_id=mid)
                        if q:
                            machine=con.execute("SELECT name FROM machines WHERE id=?",(mid,)).fetchone()
                            held.append(f'<tr><td>{h(p["name"])}</td><td>{h(p["code"])}</td><td>{label}</td><td>{h(fmt_num(q))} {h(p["unit"])}</td><td>{h(machine["name"] if machine else "—")}</td></tr>')
            body=f'<div class="welcome"><div><span class="eyebrow">ЛИЧНЫЙ КАБИНЕТ</span><h2>{h(w["name"])}</h2><p>Закреплен за станком: <b>{h(con.execute("SELECT name FROM machines WHERE id=?",(w["machine_id"],)).fetchone()[0] if w["machine_id"] else "не назначен")}</b></p></div><div class="metric"><span>Позиций на руках</span><b>{len(held)}</b></div></div><section class="panel"><div class="section-head"><h2>Инструмент на руках</h2></div><div class="table-wrap"><table><thead><tr><th>Позиция</th><th>Код</th><th>Состояние</th><th>Количество</th><th>Станок</th></tr></thead><tbody>{''.join(held) or "<tr><td colspan=5 class=empty>За рабочим пока ничего не числится</td></tr>"}</tbody></table></div></section>'
            return self.send(layout("Главная",body,user,notice,active="dashboard"))
        pcount=con.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
        low=[]; products=con.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall()
        for p in products:
            q=usable_total(con,p["id"])
            if q<float(p["min_qty"]) or q==0: low.append((p,q))
        active_held=con.execute("SELECT COUNT(DISTINCT worker_id || ':' || machine_id || ':' || product_id) FROM movements WHERE voided_at IS NULL").fetchone()[0]
        recent=con.execute("SELECT mv.*,p.name AS product,w.name AS worker,m.name AS machine,u.username FROM movements mv JOIN products p ON p.id=mv.product_id LEFT JOIN workers w ON w.id=mv.worker_id LEFT JOIN machines m ON m.id=mv.machine_id LEFT JOIN users u ON u.id=mv.actor_user_id ORDER BY mv.id DESC LIMIT 7").fetchall()
        body=f'''<section class="welcome"><div><span class="eyebrow">СВОДКА СКЛАДА</span><h2>Управляйте инструментом и его движением</h2><p>Выдача закрепляется за рабочим и станком, история операций сохраняется в журнале.</p><a class="button primary" href="/transactions">Провести операцию</a></div><div class="welcome-mark welcome-logo-crop"><img src="/static/logo.png" alt="КАТЕКС" width="150" height="150"></div></section>
        <div class="metrics"><article class="metric"><span>Активных позиций</span><b>{pcount}</b><small>инструмент и оснастка</small></article><article class="metric"><span>Ниже минимума</span><b class="{'warn' if low else ''}">{len(low)}</b><small><a class="text-link" href="/purchasing">Сформировать заявку →</a></small></article><article class="metric"><span>Рабочих</span><b>{con.execute('SELECT COUNT(*) FROM workers WHERE active=1').fetchone()[0]}</b><small>в справочнике</small></article><article class="metric"><span>Станков</span><b>{con.execute('SELECT COUNT(*) FROM machines WHERE active=1').fetchone()[0]}</b><small>в справочнике</small></article></div>
        <div class="two-col"><section class="panel"><div class="section-head"><h2>Остаток ниже минимума</h2><a class="text-link" href="/catalog">Весь каталог →</a></div><div class="table-wrap"><table><thead><tr><th>Позиция</th><th>Остаток</th><th>Минимум</th></tr></thead><tbody>{''.join(f'<tr><td><b>{h(product_primary_name(p))}</b><small>{h(product_secondary_name(p))} · {h(p["code"])}</small></td><td class="bad">{h(fmt_num(q))} {h(p["unit"])}</td><td>{h(fmt_num(p["min_qty"]))} {h(p["unit"])}</td></tr>' for p,q in low[:7]) or '<tr><td colspan=3 class=empty>Позиции ниже минимума не найдены</td></tr>'}</tbody></table></div></section>
        <section class="panel"><div class="section-head"><h2>Последние действия</h2><a class="text-link" href="/history">Весь журнал →</a></div><div class="activity-list">{''.join(self.activity_line(r) for r in recent) or '<p class="empty">Операций пока нет</p>'}</div></section></div>'''
        return self.send(layout("Главная",body,user,notice,active="dashboard"))

    def activity_line(self,r):
        labels={"receipt":"Поступление","issue":"Выдача","return":"Возврат","writeoff_stock":"Списание со склада","writeoff_issued":"Списание у рабочего","regrind_send":"Отправка на переточку","regrind_receive":"Прием после переточки"}
        return f'<div class="activity"><span class="activity-icon {h(r["kind"])}">{ {"receipt":"+","issue":"↗","return":"↙","writeoff_stock":"−","writeoff_issued":"−","regrind_send":"↗","regrind_receive":"↙"}.get(r["kind"],"•") }</span><div><b>{h("ОТМЕНЕНА · " if r["voided_at"] else "")}{h(movement_label(r["kind"],r["state_to"]))}: {h(r["product"])}</b><small>{h(r["occurred_at"].replace("T"," ")[:16])} · {h(r["worker"] or "склад")}{" · "+h(r["machine"]) if r["machine"] else ""}</small></div><strong>{h(fmt_num(r["qty"]))}</strong></div>'

    def catalog(self,con,user,notice=""):
        q=urlparse(self.path).query; query=parse_qs(q); search=query.get("q",[""])[0]; cls=query.get("class",[""])[0]; sort=query.get("sort",[""])[0]; direction=query.get("dir",["asc"])[0]
        archived=query.get("archive",[""])[0]=="1"
        sql="SELECT * FROM products WHERE active="+ ("0" if archived else "1"); args=[]
        if search: sql+=" AND (unicode_contains(name,?) OR unicode_contains(code,?) OR unicode_contains(model,?) OR unicode_contains(manufacturer,?) OR unicode_contains(category,?) OR unicode_contains(params_json,?))"; args += [search]*6
        if cls in ("consumable","fixture"): sql+=" AND item_class=?"; args.append(cls)
        extra={key:query.get(key,[""])[0].strip() for key in ("category","manufacturer","location","state","low","diameter_from","diameter_to")}
        for key in ("category","manufacturer","location"):
            if extra[key]: sql+=f" AND unicode_contains({key},?)"; args.append(extra[key])
        products=con.execute(sql+" ORDER BY category,name",args).fetchall()
        if extra["state"] in STATES: products=[p for p in products if balance(con,p["id"],extra["state"])>0]
        if extra["low"]=="1": products=[p for p in products if usable_total(con,p["id"])<p["min_qty"] or usable_total(con,p["id"])<=0]
        for key,is_min in (("diameter_from",True),("diameter_to",False)):
            if extra[key]:
                try: bound=number(extra[key])
                except ValueError: raise ValueError("Укажите числовой диаметр.")
                if not math.isfinite(bound) or bound<0: raise ValueError("Диаметр должен быть неотрицательным числом.")
                filtered=[]
                for p in products:
                    try: diameter=number(json.loads(p["params_json"] or "{}").get("diameter",""))
                    except ValueError: continue
                    if (diameter>=bound if is_min else diameter<=bound): filtered.append(p)
                products=filtered
        additional_filters=f'''<details class="catalog-extra"{" open" if any(extra.values()) else ""}><summary>Дополнительные фильтры</summary><div class="form-grid">{field("Категория","category",extra["category"])}{field("Производитель","manufacturer",extra["manufacturer"])}{field("Ячейка","location",extra["location"])}{select_field("Состояние","state",options([{"id":k,"name":v} for k,v in STATES.items()],extra["state"],"Любое состояние"))}{field("Диаметр от, мм","diameter_from",extra["diameter_from"],attrs='inputmode="decimal"')}{field("Диаметр до, мм","diameter_to",extra["diameter_to"],attrs='inputmode="decimal"')}</div><label class="filter-check"><input type="checkbox" name="low" value="1"{" checked" if extra["low"]=="1" else ""}> Только дефицит и нулевые остатки</label></details>'''

        if sort in {"diameter","l1","length"}:
            def sort_key(p):
                try: value=float(json.loads(p["params_json"] or "{}").get(sort,""))
                except (ValueError,TypeError): value=None
                return (value is None,value or 0,p["name"].lower())
            products=sorted(products,key=sort_key,reverse=(direction=="desc"))
        def sort_link(key,label):
            nxt="desc" if sort==key and direction=="asc" else "asc"
            return f'<a class="sort-link" href="/catalog?{h(urlencode({**{k:v[-1] for k,v in query.items()},"sort":key,"dir":nxt}))}">{label}{" ↕" if sort!=key else " ↓" if direction=="desc" else " ↑"}</a>'
        toggle=f'<a class="button secondary" href="/catalog">Активный каталог</a>' if archived else f'<a class="button secondary" href="/catalog?archive=1">Архив карточек</a>'
        body=f'''<section class="panel filters"><form method="get" action="/catalog" class="filter-row">{field("Поиск","q",search,attrs='placeholder="Название, код, модель, параметры"')}{'<input type="hidden" name="archive" value="1">' if archived else ''}{select_field("Группа","class",'<option value="">Все</option><option value="consumable"'+(' selected' if cls=='consumable' else '')+'>Инструмент</option><option value="fixture"'+(' selected' if cls=='fixture' else '')+'>Оснастка</option>')}<div class="catalog-filter-actions"><button class="button primary">Найти</button>{toggle}<a class="text-link" href="/catalog">Сбросить</a></div>{additional_filters}</form></section>
        <section class="panel"><div class="section-head"><div><h2>{'Архив карточек' if archived else 'Каталог инструмента'}</h2><p>{len(products)} позиций · { 'в архиве' if archived else 'нажмите на Ø, l1 или L для сортировки; шкала красная при малом запасе и зеленая при достаточном запасе'}</p></div>{'<a class="button primary" href="/product/new">+ Добавить позицию</a>' if has_permission(user,"catalog.edit") and not archived else ''}</div><div class="table-wrap"><table class="catalog-table"><thead><tr><th>Фото</th><th>Наименование / модель</th><th>Тип / категория</th><th>{sort_link("diameter","Ø, мм")}</th><th>{sort_link("l1","l1, мм")}</th><th>{sort_link("length","L, мм")}</th><th>Инв. №</th><th>Производитель</th><th>Остаток</th><th>Ячейка</th><th></th></tr></thead><tbody>{product_rows(con,products,user,archived) or '<tr><td colspan=11 class=empty>Ничего не найдено</td></tr>'}</tbody></table></div></section><script>document.querySelectorAll('.show-all').forEach(b=>b.addEventListener('click',()=>{{const selector='.'+b.dataset.target;const rows=document.querySelectorAll(selector);const open=rows.length&&rows[0].style.display==='table-row';rows.forEach(r=>r.style.display=open?'none':'table-row');b.textContent=open?'Показать все':'Скрыть';}}));</script>'''
        return self.send(layout("Каталог инструмента",body,user,notice,active="catalog"))

    def product_page(self,con,user,pid,notice=""):
        if pid==0 or self.path.endswith("/new"):
            self.require(user,"catalog.edit")
            p={"id":"","code":"","inventory_no":"","item_class":"consumable","category":"","name":"","model":"","manufacturer":"","parameters":"","params_json":"{}","unit":"шт","min_qty":"0","price_per_unit":"0","location":"","photo":None}
            title="Новая позиция"
        else:
            p=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if not p: raise KeyError(pid)
            title=p["name"]
        is_edit=bool(p["id"])
        state_rows=[]
        if is_edit:
            for st,label in STATES.items():
                held=con.execute("SELECT COALESCE(SUM(CASE WHEN kind='issue' AND state_to=? THEN qty WHEN kind IN ('return','writeoff_issued') AND state_from=? THEN -qty ELSE 0 END),0) FROM movements WHERE product_id=? AND voided_at IS NULL",(st,st,p["id"])).fetchone()[0]
                state_rows.append(f'<tr><td>{label}</td><td>{h(fmt_num(balance(con,p["id"],st)))} {h(p["unit"])}</td><td>{h(fmt_num(held))} {h(p["unit"])}</td></tr>')
        params=json.loads(p["params_json"] or "{}")
        restricted_regrind=bool(params.get("has_been_regrind"))
        can_edit=has_permission(user,"catalog.edit") and (not restricted_regrind or user["role"]=="ADMIN")
        iso=params.get("insert_iso",{})
        type_opts='<option value="consumable"'+(' selected' if p["item_class"]=="consumable" else '')+'>Инструмент</option><option value="fixture"'+(' selected' if p["item_class"]=="fixture" else '')+'>Оснастка</option>'
        quick_actions=""
        if is_edit and has_permission(user,"transactions.manage"):
            quick_actions='<div class="quick-actions">'+"".join(f'<a class="button {"primary" if op=="issue" else "secondary"}" href="/transactions?op={op}&amp;product_id={p["id"]}">{label}</a>' for op,label in [("issue","Выдать"),("receipt","Оформить поступление"),("writeoff","Списать")])+"</div>"
        body=f'''<div class="backline"><a class="text-link catalog-back" href="/catalog">← Каталог</a></div>{quick_actions}<div class="two-col detail-grid"><section class="panel"><div class="section-head"><div><span class="eyebrow">КАРТОЧКА ИНСТРУМЕНТА</span><h2>{h(title)}</h2></div>{f'<span class="tag">{h(p["code"])}</span>' if is_edit else '<span class="hint">Код присвоится автоматически</span>'}</div>
        {f'<figure class="product-media"><a class="product-photo" href="/photo/{quote(p["photo"])}" target="_blank" rel="noopener" title="Открыть фото в полном размере"><img src="/photo/{quote(p["photo"])}" alt="Фото: {h(product_primary_name(p))}"></a><figcaption>Нажмите на фото, чтобы открыть оригинал</figcaption></figure>' if is_edit and p["photo"] else ''}
        {'<form method="post" action="/product/save" enctype="multipart/form-data">'+csrf(user)+f'<input type="hidden" name="id" value="{h(p["id"])}">' if can_edit else '<fieldset disabled class="readonly-fields">'}
        {select_field("Тип позиции","item_class",type_opts,p["item_class"],True)}
        <div class="field"><span>Категория</span><select name="category" id="category" required>{category_options(p["item_class"],p["category"])}</select></div>
        {field("Своя категория","category_custom",p["category"] if p["category"] not in ITEM_CATEGORIES[p["item_class"]] else "")}
        {field("Наименование — тип инструмента","name",p["name"],attrs='placeholder="Например: фреза концевая"',required=True)}<small class="form-help">Общее название, по которому позицию удобно найти.</small>
        {field("Модель / обозначение производителя","model",p["model"],attrs='id="model-field" placeholder="Для ISO-пластины сформируется автоматически"')}<small class="form-help">Для ISO-пластины обозначение формируется автоматически; для канавочной, отрезной и резьбовой указывается заводское обозначение.</small>{field("Производитель","manufacturer",p["manufacturer"])}
        <div id="tool-parameters">{material_group_field(selected=params.get('material_groups') or ([params.get('material_group')] if params.get('material_group') else []))}{category_parameter_fields(params)}{category_parameter_fields(params,item_class='fixture')}</div>
        <div id="insert-iso" hidden><h3>Сменная токарная пластина — обозначение ISO 1832 / ГОСТ 19042</h3>{iso_insert_fields(iso=iso)}</div>
        <div id="fixture-number" {'hidden' if p['item_class']!='fixture' else ''}>{field("Инвентарный номер оснастки","inventory_no",p["inventory_no"],required=p["item_class"]=="fixture")}</div>
        <div class="form-grid">{field("Стоимость за 1 шт., руб.","price_per_unit",fmt_num(p["price_per_unit"]),typ="number",attrs='min="0" step="1"')}{field("Единица учета","unit",p["unit"],required=True)}{field("Минимальный остаток, шт","min_qty",0 if restricted_regrind else fmt_num(p["min_qty"]),typ="number",attrs='min="0" max="0" readonly' if restricted_regrind else 'min="0" step="1"')}{field("Ячейка / место хранения","location",p["location"])}</div>
        {'<label class="field"><span>Фото позиции</span><input type="file" name="photo" accept="image/png,image/jpeg,image/webp"></label>' if can_edit else ''}
        {'<button class="button primary">Сохранить карточку</button></form>' if can_edit else (f'<p class="form-help">Параметры карточки после переточки может изменять только администратор.</p></fieldset>' if restricted_regrind else '</fieldset>')}</section>
        <section class="panel"><div class="section-head"><h2>Остатки по состоянию</h2><a class="text-link" href="/transactions?op=issue&amp;product_id={p["id"]}">Провести операцию →</a></div><table><thead><tr><th>Состояние</th><th>В учете</th><th>У рабочих</th></tr></thead><tbody>{''.join(state_rows) or '<tr><td colspan=3>Сохраните позицию и внесите поступление</td></tr>'}</tbody></table><h3>Параметры инструмента</h3><div class="parameter-summary">{category_parameter_summary(params)}</div><p><b>Инвентарный номер:</b> {h(p["inventory_no"] or "не присваивается инструменту")}</p><p><b>Место хранения:</b> {h(p["location"]) or "не указано"}</p></section></div>
        <script>
        const cats={script_json(ITEM_CATEGORIES)};
        function refreshFixtureParams(prefix){{const group=document.querySelector('[name="'+prefix+'fixture_group"]'),type=document.querySelector('[name="'+prefix+'fixture_type"]');if(!group||!type)return;const sets={{turning:['bmt_block','bot_block','vdi_block','turn_holder','transition_bushing','turn_collet','turn_chuck'],milling:['mill_holder','mill_chuck','mill_collet','vise','boring','angle_head'],general:['transition_bushing','vise','boring','other']}};const valid=sets[group.value]||[];Array.from(type.options).forEach(o=>{{if(!o.value)return;const show=valid.includes(o.value);o.hidden=!show;o.disabled=!show;}});if(type.value&&!valid.includes(type.value))type.value='';const detail={{bmt_block:'block',bot_block:'block',vdi_block:'block',turn_holder:'holder',transition_bushing:'bushing',vise:'vise',boring:'boring',mill_holder:'mill',mill_chuck:'mill',mill_collet:'mill',angle_head:'mill',turn_collet:'common',turn_chuck:'common',other:'common'}}[type.value]||'common';document.querySelectorAll('[data-fixture-detail]').forEach(x=>{{const show=x.dataset.fixtureDetail===detail;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>el.disabled=!show);}});const interfaces=document.querySelector('[name="'+prefix+'interface"]'),allowed=group.value==='turning'?['BMT','BOT','VDI','Цилиндрический','Прочее']:group.value==='milling'?['BT','BBT','SK','HSK','Morse','Цилиндрический','Прочее']:['Цилиндрический','Прочее'];if(interfaces){{Array.from(interfaces.options).forEach(o=>{{if(!o.value)return;const show=allowed.includes(o.value);o.hidden=!show;o.disabled=!show;}});if(interfaces.value&&!allowed.includes(interfaces.value))interfaces.value='';}}}}
        function refreshCardType(){{const f=document.querySelector('[name=item_class]').value==='fixture', sel=document.getElementById('category'), old=sel.value;document.getElementById('fixture-number').hidden=!f;document.getElementById('tool-parameters').hidden=false;document.querySelector('[name=inventory_no]').required=f;sel.innerHTML='<option value="">Выберите категорию</option>'+cats[f?'fixture':'consumable'].map(x=>'<option>'+x+'</option>').join('')+'<option value="__custom__">Своя категория…</option>';if(cats[f?'fixture':'consumable'].includes(old))sel.value=old;sel.dispatchEvent(new Event('change'));}}
        document.querySelector('[name=item_class]').addEventListener('change',refreshCardType);
        function refreshCategory(){{const cat=document.getElementById('category').value.trim().toLowerCase(),fixture=document.querySelector('[name=item_class]').value==='fixture',plate=['пластина','пластины','пластина токарная','токарная пластина','резьбовая пластина'].includes(cat),body=['фреза','резьбофреза','борштанга','зенкер','зенковка','резец','пила'].includes(cat),thread=['резьбофреза','метчик','плашка'].includes(cat),axial=['сверло','метчик','развертка','зенкер','зенковка','резьбофреза'].includes(cat);document.getElementById('tool-parameters').hidden=false;document.querySelectorAll('.parameter-family').forEach(x=>{{const fam=x.dataset.family,show=fam==='fixture'?fixture:fam==='geometry'?!plate&&!fixture:fam==='plate'?plate:fam==='insert'?body&&!fixture:fam==='thread'?thread&&!fixture:fam==='coolant'?axial&&!fixture:false;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>el.disabled=!show);}});document.querySelector('.iso513-field').hidden=fixture;document.querySelectorAll('.iso513-field input').forEach(x=>x.disabled=fixture);const custom=document.querySelector('[name=category_custom]');custom.closest('.field').hidden=document.getElementById('category').value!=='__custom__';custom.required=document.getElementById('category').value==='__custom__';const kind=document.querySelector('[name=insert_kind]');if(plate&&kind&&!kind.value)kind.value=cat==='резьбовая пластина'?'thread':'iso';const selectedKind=kind?.value||'iso';document.querySelectorAll('[data-insert-kind]').forEach(x=>{{const show=selectedKind==='thread'?x.dataset.insertKind==='thread':['grooving','parting'].includes(selectedKind)?x.dataset.insertKind==='special':false;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>{{el.disabled=!show;el.required=show&&(el.name.endsWith('thread_insert_designation')||el.name.endsWith('plate_designation'));}});}});const isoOn=plate&&selectedKind==='iso',box=document.getElementById('insert-iso'),model=document.getElementById('model-field');box.hidden=!isoOn;box.querySelectorAll('select,input').forEach(x=>{{x.disabled=!isoOn;x.required=isoOn;}});model.readOnly=isoOn;const keys=['shape','clearance','tolerance','fixing','size','thickness','corner'],value=keys.map(k=>document.querySelector('[name=iso_'+k+']').value).join('').toUpperCase();document.getElementById('iso-preview').textContent=value||'—';if(isoOn)model.value=value;else if(plate)model.value=selectedKind==='thread'?(document.querySelector('[name=thread_insert_designation]')?.value||''):(document.querySelector('[name=plate_designation]')?.value||'');refreshFixtureParams('');}}
        document.getElementById('category').addEventListener('change',refreshCategory);document.querySelector('[name=item_class]').addEventListener('change',refreshCategory);document.querySelector('[name=insert_kind]')?.addEventListener('change',refreshCategory);document.querySelector('[name=fixture_group]')?.addEventListener('change',()=>refreshFixtureParams(''));document.querySelector('[name=fixture_type]')?.addEventListener('change',()=>refreshFixtureParams(''));document.getElementById('insert-iso').addEventListener('input',refreshCategory);document.getElementById('insert-iso').addEventListener('change',refreshCategory);document.querySelectorAll('[name=thread_insert_designation],[name=plate_designation]').forEach(el=>el.addEventListener('input',()=>document.querySelector('[name=model]').value=el.value));refreshCategory();
        </script>'''
        return self.send(layout("Каталог инструмента",body,user,notice,active="catalog"))

    def save_product(self,con,user,data,files):
        self.require(user,"catalog.edit")
        existing_card=None
        if data.get("id"):
            existing_card=con.execute("SELECT * FROM products WHERE id=? AND active=1",(int(data["id"]),)).fetchone()
            if not existing_card: raise ValueError("Позиция не найдена.")
            existing_params=json.loads(existing_card["params_json"] or "{}")
            if existing_params.get("has_been_regrind") and user["role"]!="ADMIN":
                raise PermissionError("Параметры карточки после переточки может изменять только администратор.")
        item_class=data.get("item_class")
        if item_class not in ("consumable","fixture"): raise ValueError("Выберите тип позиции.")
        category=data.get("category_custom","").strip() if data.get("category")=="__custom__" else data.get("category","").strip()
        name=data.get("name","").strip(); unit=data.get("unit","").strip()
        if not category or not name or not unit: raise ValueError("Заполните категорию, наименование и единицу учета.")
        if item_class=="consumable" and is_insert_category(category) and unit.casefold()!="шт": raise ValueError("Для сменных пластин единица учета должна быть «шт», чтобы корректно суммировать остаток.")
        try: minimum=float(data.get("min_qty") or 0)
        except: raise ValueError("Минимальный остаток должен быть целым числом.")
        if minimum<0 or not minimum.is_integer(): raise ValueError("Минимальный остаток должен быть целым неотрицательным числом.")
        minimum=int(minimum)
        if existing_card and json.loads(existing_card["params_json"] or "{}").get("has_been_regrind") and minimum!=0:
            raise ValueError("Для карточки после переточки минимальный остаток должен быть равен 0.")
        try: price=float(data.get("price_per_unit") or 0)
        except: raise ValueError("Стоимость должна быть числом больше или равным нулю.")
        if not math.isfinite(price) or price<0 or not price.is_integer(): raise ValueError("Стоимость за 1 шт. должна быть целым числом рублей.")
        price=int(price)
        inventory_no=data.get("inventory_no","").strip() if item_class=="fixture" else None
        if item_class=="fixture" and not inventory_no: raise ValueError("Для оснастки укажите инвентарный номер.")
        params=normalized_category_params(data,"",category,item_class)
        if existing_card:
            existing_params=json.loads(existing_card["params_json"] or "{}")
            for meta_key in ("has_been_regrind","regrind_source_code"):
                if meta_key in existing_params: params[meta_key]=existing_params[meta_key]
        groups=data.get("material_groups",[])
        if isinstance(groups,str): groups=[groups]
        groups=list(dict.fromkeys(str(g).strip().upper() for g in groups if str(g).strip())) if item_class=="consumable" else []
        if any(group not in ISO_513_GROUPS for group in groups): raise ValueError("Выберите группы материала по ISO 513: P, M, K, N, S или H.")
        if groups:
            params["material_groups"]=groups
            params["material_group"]=groups[0]
        else:
            params.pop("material_groups",None); params.pop("material_group",None)
        model=data.get("model","").strip()
        if item_class=="consumable" and is_insert_category(category) and params.get("insert_kind")!="thread":
            model=insert_base_code(params)
        elif item_class=="consumable" and is_insert_category(category):
            model=params.get("thread_insert_designation","")
        legacy_parameters="; ".join(f'{label}: {params[key]}' for key,label in TOOL_PARAMETER_FIELDS if params.get(key))
        legacy_parameters += ("; " if legacy_parameters and (params.get("insert_designation") or params.get("thread_insert_designation")) else "") + (f'Сменная пластина: {params.get("insert_designation")}' if params.get("insert_designation") else f'Резьбовая пластина: {params.get("thread_insert_designation")}' if params.get("thread_insert_designation") else "")
        photo_name=None; upload=files.get("photo")
        if upload and upload[1]:
            filename,content,mime=upload; ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}.get(mime)
            if not ext or len(content)>5*1024*1024: raise ValueError("Фото должно быть JPG, PNG или WebP и не больше 5 МБ.")
            photo_name=secrets.token_hex(16)+"."+ext; (UPLOAD_DIR/photo_name).write_bytes(content)
        values=(item_class,category,name,model,data.get("manufacturer","").strip(),legacy_parameters,json.dumps(params,ensure_ascii=False),inventory_no,unit,minimum,price,data.get("location","").strip(),photo_name)
        if data.get("id"):
            pid=int(data["id"]); existing=existing_card
            if not photo_name: values=values[:-1]+(existing["photo"],)
            con.execute("UPDATE products SET item_class=?,category=?,name=?,model=?,manufacturer=?,parameters=?,params_json=?,inventory_no=?,unit=?,min_qty=?,price_per_unit=?,location=?,photo=? WHERE id=?",values+(pid,))
            return self.redirect(f"/product/{pid}","Карточка обновлена")
        cur=con.execute("""INSERT INTO products(code,item_class,category,name,model,manufacturer,parameters,params_json,inventory_no,unit,min_qty,price_per_unit,location,photo,created_at)
          VALUES('PENDING',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",values+(now_iso(),))
        code=f'{"INS" if item_class=="consumable" else "OSN"}-{cur.lastrowid:06d}'
        con.execute("UPDATE products SET code=? WHERE id=?",(code,cur.lastrowid))
        return self.redirect(f"/product/{cur.lastrowid}","Позиция добавлена в каталог")

    def transactions(self,con,user,notice=""):
        self.require(user,"transactions.manage")
        workers=[dict(r) for r in con.execute("SELECT w.id,w.name,w.machine_id,m.name machine FROM workers w LEFT JOIN machines m ON m.id=w.machine_id WHERE w.active=1 ORDER BY w.name")]
        worker_map={}
        for w in workers:
            machines=[{"id":r["id"],"name":r["name"]} for r in con.execute("SELECT m.id,m.name FROM worker_machines wm JOIN machines m ON m.id=wm.machine_id WHERE wm.worker_id=? AND m.active=1 ORDER BY m.name",(w["id"],))]
            worker_map[str(w["id"]) ]={"machine_id":w["machine_id"],"machine":w["machine"] or "Станок не назначен","machines":machines}
        category_json=script_json(ITEM_CATEGORIES)
        queue_items=products_in_state(con,"regrind_queue")
        queue_snapshot=json.dumps([[r["id"],balance(con,r["id"],"regrind_queue")] for r in con.execute("SELECT id FROM products WHERE active=1 ORDER BY id") if balance(con,r["id"],"regrind_queue")>0],separators=(",",":"))
        regrind_items=products_in_state(con,"regrind")
        queue_opts=options(queue_items,None,"Инструмента в очереди нет")
        regrind_opts=options(regrind_items,None,"Инструмент на переточке не числится")
        regrind_params=script_json({str(p["id"]):p["params"] for p in regrind_items})
        regrind_param_attrs='min="0" step="any"'+(' readonly' if user["role"]!="ADMIN" else '')
        card_fields=f'''<div id="new-product-fields" hidden><div class="form-grid">{select_field('Тип позиции','new_item_class','<option value="consumable">Инструмент</option><option value="fixture">Оснастка</option>','consumable')}<div class="field"><span>Категория</span><select name="new_category" id="new-category"><option value="">Выберите категорию</option>{''.join(f'<option>{h(x)}</option>' for x in ITEM_CATEGORIES['consumable'])}<option value="__custom__">Своя категория…</option></select></div></div>{field('Своя категория','new_category_custom')}<div class="form-grid">{field('Наименование — тип инструмента','new_name',attrs='placeholder="Например: фреза концевая"')}{field('Модель / обозначение производителя','new_model',attrs='id="new-model-field" placeholder="Для ISO-пластины сформируется автоматически"')}</div>{field('Производитель','new_manufacturer')}<div id="new-tool-params">{material_group_field('new_material_groups')}{category_parameter_fields({},'new_')}{category_parameter_fields({},'new_','fixture')}</div><div id="new-insert-iso" hidden><h3>Сменная токарная пластина — ISO 1832 / ГОСТ 19042</h3>{iso_insert_fields('new_')}</div><div id="new-fixture-no" hidden>{field('Инвентарный номер оснастки','new_inventory_no')}</div><div class="form-grid">{field('Стоимость за 1 шт., руб.','new_price_per_unit','0',typ='number',attrs='min="0" step="1"')}{field('Единица учета','new_unit','шт')}{field('Минимальный остаток, шт','new_min_qty','0',typ='number',attrs='min="0" step="1"')}{field('Ячейка / место хранения','new_location')}</div><label class="field"><span>Фото позиции</span><input type="file" name="new_photo" accept="image/png,image/jpeg,image/webp"></label><p class="form-help">Карточка и поступление будут созданы одним действием.</p></div>'''
        body=f'''<div class="transaction-grid"><section class="panel"><div class="section-head"><div><span class="eyebrow">СКЛАД</span><h2>Поступление</h2></div><span class="op-icon receipt">+</span></div><form method="post" enctype="multipart/form-data" action="/transaction/receipt" id="receipt-form">{csrf(user)}<div class="product-picker" id="receipt-picker"><label class="field"><span>Выбрать существующий</span><input id="receipt-search" type="search" autocomplete="off" placeholder="Начните вводить название, код или модель"><input type="hidden" name="product_id" id="receipt-product-id"><div class="search-results" id="receipt-results" role="listbox" hidden></div></label><button type="button" class="button secondary" id="add-receipt-product">Добавить новый</button></div><p id="receipt-selected" class="picked-product" hidden></p>{card_fields}<div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}</div>{field('Документ / комментарий','note')}<button class="button primary">Зафиксировать поступление</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">ЗАКРЕПЛЕНИЕ</span><h2>Выдача</h2></div><span class="op-icon issue">↗</span></div><form method="post" action="/transaction/issue" id="issue-form">{csrf(user)}<div class="product-picker"><label class="field"><span>Инструмент или оснастка</span><input id="issue-search" type="search" autocomplete="off" placeholder="Начните вводить название, код или модель"><input type="hidden" name="product_id" id="issue-product-id"><div class="search-results" id="issue-results" role="listbox" hidden></div></label></div><p id="issue-selected" class="picked-product" hidden></p>{select_field('Рабочий','worker_id',worker_select(con),required=True)}<label class="field"><span>Станок выдачи</span><select name="machine_id" id="issue-machine-select" required disabled><option value="">Сначала выберите рабочего</option></select></label><p class="machine-assignment">Станок рабочего: <b id="issue-machine">Сначала выберите рабочего</b></p><div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}{select_field('Состояние','state','<option value="">Выберите позицию</option>',required=True)}</div><p id="issue-state-warning" class="state-warning" hidden>! Остаток есть в нескольких состояниях — выберите состояние выдачи.</p>{field('Комментарий','note')}<button class="button primary">Выдать</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">ЗАКРЫТИЕ ВЫДАЧИ</span><h2>Принять сдачу</h2></div><span class="op-icon return">↙</span></div><form method="post" action="/transaction/settle" id="settle-form">{csrf(user)}{select_field('Рабочий, который сдает','worker_id',worker_select(con),required=True)}<p class="machine-assignment">Станок: <b id="settle-machine">Выберите рабочего</b></p><div class="field"><span>Инструмент или оснастка на руках</span><select name="holding_key" id="holding-select" required disabled><option value="">Сначала выберите рабочего</option></select></div><p id="held-amount" class="form-help">Покажу количество, которое сейчас числится за выбранным рабочим.</p><input type="hidden" name="product_id"><input type="hidden" name="state_from"><input type="hidden" name="machine_id"><div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}{select_field('Действие','action','<option value="return">Вернуть как БУ на склад</option><option value="regrind">Передать в очередь на переточку</option><option value="writeoff">Списать у рабочего</option>')}</div><div id="return-state"><p class="form-help">Обычный возврат будет учтен отдельно от нового инструмента как БУ.</p></div>{field('Причина / комментарий','note')}<button class="button primary">Принять сдачу</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">УТРАТА / БРАК</span><h2>Списание со склада</h2></div><span class="op-icon writeoff">−</span></div><form method="post" action="/transaction/writeoff" id="writeoff-form">{csrf(user)}<div class="product-picker"><label class="field"><span>Инструмент или оснастка</span><input id="writeoff-search" type="search" autocomplete="off" placeholder="Начните вводить название, код или модель"><input type="hidden" name="product_id" id="writeoff-product-id"><div class="search-results" id="writeoff-results" role="listbox" hidden></div></label></div><p id="writeoff-selected" class="picked-product" hidden></p><div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}{select_field('Состояние','state','<option value="">Выберите позицию</option>',required=True)}</div>{field('Причина списания','note',required=True)}<button class="button secondary">Списать</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">РЕМОНТ ИНСТРУМЕНТА</span><h2>Отправка на переточку</h2><p>{len(queue_items)} позиций ожидают отправки</p></div><div class="regrind-actions"><a class="button secondary" href="/regrind/export">Выгрузить список</a><form method="post" action="/transaction/regrind/send-all">{csrf(user)}<input type="hidden" name="queue_snapshot" value="{h(queue_snapshot)}"><button class="button primary" {'disabled' if not queue_items else ''}>Отправить все позиции</button></form></div></div><form method="post" action="/transaction/regrind/send">{csrf(user)}{select_field('Позиция из очереди','product_id',queue_opts,required=True)}<div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}{field('Документ / комментарий','note')}</div><button class="button primary">Отметить отправку</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">ВОЗВРАТ ОТ ПОДРЯДЧИКА</span><h2>Принять после переточки</h2><p>{'Укажите обновленные Ø и l1 при приемке.' if user['role']=='ADMIN' else 'Размеры после переточки доступны для изменения администратору.'}</p></div></div><form method="post" action="/transaction/regrind/receive" id="regrind-receive-form">{csrf(user)}{select_field('Инструмент на переточке','product_id',regrind_opts,required=True)}<div class="form-grid">{field('Количество, шт','qty',typ='number',attrs='min="1" step="1"',required=True)}{field('Ø после переточки, мм','diameter',typ='number',attrs=regrind_param_attrs)}{field('l1 после переточки, мм','l1',typ='number',attrs=regrind_param_attrs)}</div>{'<p class="form-help">Параметры после переточки может изменять администратор. После приемки он сможет обновить карточку.</p>' if user["role"]!="ADMIN" else ''}{field('Документ / комментарий','note')}<button class="button primary">Принять на склад</button></form></section></div>
        <script>
        const workerMap={script_json(worker_map)};const categories={category_json};
        const issueWorker=document.querySelector('#issue-form [name=worker_id]'),issueMachine=document.getElementById('issue-machine-select');function updateIssueMachines(){{const v=workerMap[issueWorker.value],machines=v?.machines||[];issueMachine.replaceChildren(new Option(machines.length>1?'Выберите станок':machines.length?'Станок':'У рабочего не назначен',''));for(const m of machines)issueMachine.add(new Option(m.name,m.id));issueMachine.disabled=!machines.length;if(machines.length===1)issueMachine.value=String(machines[0].id);document.getElementById('issue-machine').textContent=machines.length===1?machines[0].name:machines.length?'Выберите станок рабочего':'У рабочего не назначен';}}issueWorker.addEventListener('change',updateIssueMachines);issueMachine.addEventListener('change',()=>{{document.getElementById('issue-machine').textContent=issueMachine.selectedOptions[0]?.textContent||'Выберите станок рабочего';}});
        const card=document.getElementById('new-product-fields'), receiptSearch=document.getElementById('receipt-search'), receiptId=document.getElementById('receipt-product-id'), receiptSelected=document.getElementById('receipt-selected');
        function receiptMode(on){{card.hidden=!on;card.querySelectorAll('input,select').forEach(x=>x.disabled=!on);card.querySelectorAll('[name=new_name],[name=new_unit],[name=new_category]').forEach(x=>x.required=on);}}
        receiptMode(false);document.getElementById('add-receipt-product').addEventListener('click',()=>{{receiptId.value='new';receiptSearch.value='';receiptSelected.hidden=true;receiptMode(true);refreshNewType();}});
        function attachPicker(prefix, onChoose){{const input=document.getElementById(prefix+'-search'), results=document.getElementById(prefix+'-results'), hidden=document.getElementById(prefix+'-product-id');let timer=0,request=0;
          window.productPickers=window.productPickers||{{}};window.pickedProducts=window.pickedProducts||{{}};
          function choose(item){{hidden.value=item.id;input.value=item.name+' · '+item.code;results.hidden=true;const p=document.getElementById(prefix+'-selected');if(p){{p.textContent='Остаток: '+(item.stocks.map(s=>s.label+' — '+s.qty+' шт').join('; ')||'нет')+' · Место хранения: '+(item.location||'не указано');p.hidden=false;}}window.pickedProducts[prefix]=item;onChoose?.(item);}}
          window.productPickers[prefix]=choose;
          function clearPicked(){{delete window.pickedProducts[prefix];hidden.value='';const p=document.getElementById(prefix+'-selected');if(p)p.hidden=true;if(prefix==='receipt')receiptMode(false);if(prefix==='issue')setStates(null);if(prefix==='writeoff')setStates(null,true);}}
          input.addEventListener('input',()=>{{clearPicked();clearTimeout(timer);const n=++request;results.hidden=true;const q=input.value.trim();if(q.length<2){{results.hidden=true;results.replaceChildren();return;}}timer=setTimeout(async()=>{{try{{const r=await fetch('/api/products?q='+encodeURIComponent(q)+'&mode='+prefix);if(!r.ok)throw new Error();const items=await r.json();if(n!==request)return;results.replaceChildren();if(!items.length){{const empty=document.createElement('div');empty.className='search-empty';empty.textContent='Ничего не найдено';results.append(empty);}}for(const item of items){{const b=document.createElement('button');b.type='button';b.className='search-option'+(item.regrind_variant?' regrind-option':'');b.setAttribute('role','option');const title=document.createElement('strong');title.textContent=item.name+' · '+item.code;b.append(title);const sub=document.createElement('small');sub.textContent=[item.regrind_variant?'После переточки':'Основная карточка',item.product_name,item.category,item.manufacturer].filter((v,i,a)=>v&&a.indexOf(v)===i).join(' · ');b.append(sub);b.addEventListener('click',()=>choose(item));results.append(b);}}results.hidden=false;}}catch(e){{if(n!==request)return;results.replaceChildren();const error=document.createElement('div');error.className='search-empty';error.textContent='Не удалось загрузить. Повторите поиск.';results.append(error);results.hidden=false;}}}},180);}});
          input.addEventListener('keydown',e=>{{const buttons=[...results.querySelectorAll('button')];if(e.key==='Escape'){{results.hidden=true;return;}}if((e.key==='ArrowDown'||e.key==='Enter')&&!results.hidden&&buttons.length){{e.preventDefault();buttons[0].focus();}}}});results.addEventListener('keydown',e=>{{const buttons=[...results.querySelectorAll('button')],i=buttons.indexOf(document.activeElement);if(e.key==='Escape'){{results.hidden=true;input.focus();}}else if(e.key==='ArrowDown'||e.key==='ArrowUp'){{e.preventDefault();buttons[(i+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();}}}});
          input.addEventListener('focus',()=>{{if(results.childElementCount&&input.value.trim().length>=2)results.hidden=false;}});document.addEventListener('click',e=>{{if(!e.target.closest('#'+prefix+'-picker')&&!e.target.closest('.product-picker'))results.hidden=true;}});
        }}
        function setStates(item, writeoff=false){{const form=document.getElementById(writeoff?'writeoff-form':'issue-form');if(!form)return;const sel=form.elements.state,warning=writeoff?null:document.getElementById('issue-state-warning');sel.replaceChildren();if(!item){{sel.add(new Option('Выберите позицию',''));sel.disabled=true;if(warning)warning.hidden=true;return;}}const states=item.stocks||[];if(!states.length){{sel.add(new Option('Нет доступного остатка',''));sel.disabled=true;if(warning)warning.hidden=true;return;}}if(states.length>1)sel.add(new Option('Выберите состояние',''));for(const s of states)sel.add(new Option(s.label+' — '+s.qty+' шт',s.state));if(states.length===1)sel.value=states[0].state;sel.disabled=false;if(warning)warning.hidden=states.length<2;}}
        attachPicker('receipt',item=>{{receiptMode(false);receiptSelected.textContent=[item.name,item.code,item.product_name,item.category].filter((v,i,a)=>v&&a.indexOf(v)===i).join(' · ');receiptSelected.hidden=false;}});
        attachPicker('issue',item=>setStates(item));attachPicker('writeoff',item=>setStates(item,true));setStates(null);setStates(null,true);
        const newType=document.querySelector('[name=new_item_class]'), newCat=document.getElementById('new-category');function refreshNewType(){{const f=newType.value==='fixture',old=newCat.value;document.getElementById('new-fixture-no').hidden=!f;document.querySelector('[name=new_inventory_no]').required=f;newCat.innerHTML='<option value="">Выберите категорию</option>'+categories[f?'fixture':'consumable'].map(x=>'<option>'+x+'</option>').join('')+'<option value="__custom__">Своя категория…</option>';if(categories[f?'fixture':'consumable'].includes(old))newCat.value=old;newCat.dispatchEvent(new Event('change'));}}function refreshNewFixtureParams(){{const group=document.querySelector('[name=new_fixture_group]'),type=document.querySelector('[name=new_fixture_type]');if(!group||!type)return;const sets={{turning:['bmt_block','bot_block','vdi_block','turn_holder','transition_bushing','turn_collet','turn_chuck'],milling:['mill_holder','mill_chuck','mill_collet','vise','boring','angle_head'],general:['transition_bushing','vise','boring','other']}};const valid=sets[group.value]||[];Array.from(type.options).forEach(o=>{{if(!o.value)return;const show=valid.includes(o.value);o.hidden=!show;o.disabled=!show;}});if(type.value&&!valid.includes(type.value))type.value='';const detail={{bmt_block:'block',bot_block:'block',vdi_block:'block',turn_holder:'holder',transition_bushing:'bushing',vise:'vise',boring:'boring',mill_holder:'mill',mill_chuck:'mill',mill_collet:'mill',angle_head:'mill',turn_collet:'common',turn_chuck:'common',other:'common'}}[type.value]||'common';document.querySelectorAll('#new-tool-params [data-fixture-detail]').forEach(x=>{{const show=x.dataset.fixtureDetail===detail;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>el.disabled=!show);}});const sel=document.querySelector('[name=new_interface]'),allowed=group.value==='turning'?['BMT','BOT','VDI','Цилиндрический','Прочее']:group.value==='milling'?['BT','BBT','SK','HSK','Morse','Цилиндрический','Прочее']:['Цилиндрический','Прочее'];if(sel){{Array.from(sel.options).forEach(o=>{{if(!o.value)return;const show=allowed.includes(o.value);o.hidden=!show;o.disabled=!show;}});if(sel.value&&!allowed.includes(sel.value))sel.value='';}}}}
        function refreshNewCategory(){{const cat=newCat.value.trim().toLowerCase(),fixture=newType.value==='fixture',plate=['пластина','пластины','пластина токарная','токарная пластина','резьбовая пластина'].includes(cat),body=['фреза','резьбофреза','борштанга','зенкер','зенковка','резец','пила'].includes(cat),thread=['резьбофреза','метчик','плашка'].includes(cat),axial=['сверло','метчик','развертка','зенкер','зенковка','резьбофреза'].includes(cat);document.querySelectorAll('#new-tool-params .parameter-family').forEach(x=>{{const fam=x.dataset.family,show=fam==='fixture'?fixture:fam==='geometry'?!plate&&!fixture:fam==='plate'?plate:fam==='insert'?body&&!fixture:fam==='thread'?thread&&!fixture:fam==='coolant'?axial&&!fixture:false;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>el.disabled=!show);}});const groups=document.querySelector('.iso513-field');groups.hidden=fixture;groups.querySelectorAll('input').forEach(x=>x.disabled=fixture);const custom=document.querySelector('[name=new_category_custom]');custom.required=newCat.value==='__custom__';custom.closest('.field').hidden=!custom.required;const kind=document.querySelector('[name=new_insert_kind]');if(plate&&kind&&!kind.value)kind.value=cat==='резьбовая пластина'?'thread':'iso';const selectedKind=kind?.value||'iso';document.querySelectorAll('#new-tool-params [data-insert-kind]').forEach(x=>{{const show=selectedKind==='thread'?x.dataset.insertKind==='thread':['grooving','parting'].includes(selectedKind)?x.dataset.insertKind==='special':false;x.hidden=!show;x.querySelectorAll('input,select').forEach(el=>{{el.disabled=!show;el.required=show&&(el.name.endsWith('thread_insert_designation')||el.name.endsWith('plate_designation'));}});}});const isoOn=plate&&selectedKind==='iso',box=document.getElementById('new-insert-iso'),model=document.getElementById('new-model-field');box.hidden=!isoOn;box.querySelectorAll('select,input').forEach(x=>{{x.disabled=!isoOn;x.required=isoOn;}});model.readOnly=isoOn;const keys=['shape','clearance','tolerance','fixing','size','thickness','corner'],code=keys.map(k=>document.querySelector('[name=new_iso_'+k+']').value).join('').toUpperCase();document.getElementById('new_iso-preview').textContent=code||'—';if(isoOn)model.value=code;else if(plate)model.value=selectedKind==='thread'?(document.querySelector('[name=new_thread_insert_designation]')?.value||''):(document.querySelector('[name=new_plate_designation]')?.value||'');refreshNewFixtureParams();}}newType.addEventListener('change',refreshNewType);newCat.addEventListener('change',refreshNewCategory);document.querySelector('[name=new_insert_kind]')?.addEventListener('change',refreshNewCategory);document.querySelector('[name=new_fixture_group]')?.addEventListener('change',refreshNewFixtureParams);document.querySelector('[name=new_fixture_type]')?.addEventListener('change',refreshNewFixtureParams);document.getElementById('new-insert-iso').addEventListener('input',refreshNewCategory);document.getElementById('new-insert-iso').addEventListener('change',refreshNewCategory);document.querySelectorAll('[name=new_thread_insert_designation],[name=new_plate_designation]').forEach(el=>el.addEventListener('input',()=>document.querySelector('[name=new_model]').value=el.value));refreshNewCategory();
        const settleWorker=document.querySelector('#settle-form [name=worker_id]'), heldSelect=document.getElementById('holding-select');let heldRows=[],heldRequest=0;
        function resetHolding(){{const f=document.getElementById('settle-form');for(const key of ['product_id','state_from','machine_id','qty'])f.elements[key].value='';f.elements.qty.removeAttribute('max');}}
        async function loadHeld(){{const n=++heldRequest;resetHolding();heldRows=[];heldSelect.replaceChildren(new Option('Загрузка…',''));heldSelect.disabled=true;const w=workerMap[settleWorker.value];document.getElementById('settle-machine').textContent=w?w.machine:'Выберите рабочего';if(!w){{heldSelect.replaceChildren(new Option('Сначала выберите рабочего',''));return;}}try{{const response=await fetch('/api/held?worker_id='+encodeURIComponent(settleWorker.value));if(!response.ok)throw new Error();const rows=await response.json();if(n!==heldRequest)return;heldRows=rows;heldSelect.replaceChildren(new Option('Выберите позицию',''));heldRows.forEach((x,i)=>heldSelect.add(new Option(x.name+' · '+x.code+' · '+x.state_name+' — числится '+x.qty+' шт · '+x.machine,String(i))));heldSelect.disabled=!heldRows.length;document.getElementById('held-amount').textContent=heldRows.length?'Выберите позицию и укажите количество сдачи.':'За этим рабочим инструмент не числится.';}}catch(e){{if(n!==heldRequest)return;heldSelect.replaceChildren(new Option('Не удалось загрузить. Выберите рабочего повторно.',''));document.getElementById('held-amount').textContent='Ошибка связи с сервером.';}}}}
        settleWorker.addEventListener('change',loadHeld);heldSelect.addEventListener('change',()=>{{resetHolding();if(heldSelect.value==='')return;const x=heldRows[Number(heldSelect.value)],form=document.getElementById('settle-form');if(!x)return;form.elements.product_id.value=x.product_id;form.elements.state_from.value=x.state;form.elements.machine_id.value=x.machine_id;form.elements.qty.max=x.qty;document.getElementById('held-amount').textContent='На руках: '+x.qty+' шт. Станок: '+x.machine+'.';}});document.querySelector('#settle-form [name=action]').addEventListener('change',e=>{{document.getElementById('return-state').hidden=e.target.value!=='return';document.querySelector('#settle-form [name=note]').required=e.target.value==='writeoff';}});
        const regrindParams={regrind_params};const grSelect=document.querySelector('#regrind-receive-form [name=product_id]');grSelect.addEventListener('change',()=>{{const p=regrindParams[grSelect.value]||{{}};document.querySelector('#regrind-receive-form [name=diameter]').value=p.diameter||'';document.querySelector('#regrind-receive-form [name=l1]').value=p.l1||'';}});
        </script>'''
        operations=[("receipt","Поступление"),("issue","Выдача"),("settle","Возврат / сдача"),("writeoff","Списание"),("regrind-send","Отправка на переточку"),("regrind-receive","Приём с переточки")]
        query=parse_qs(urlparse(self.path).query)
        selected=query.get("op",["receipt"])[0]
        if selected not in dict(operations): selected="receipt"
        index=iter(operations)
        def operation_section(match):
            op,label=next(index)
            return f'<section class="panel operation-panel" id="op-{op}" data-operation="{op}" aria-labelledby="tab-{op}"{ " hidden" if op!=selected else ""}>'
        body=re.sub(r'<section class="panel">',operation_section,body)
        body=body.replace('class="transaction-grid"','class="transaction-grid operation-workspace"')
        tabs='<div class="operation-intro"><p class="eyebrow">РАБОЧЕЕ МЕСТО КЛАДОВЩИКА</p><h2>Выберите операцию</h2><p>Одна задача на экране. Проверьте данные перед проведением — операция сохранится в журнале.</p></div><nav class="operation-tabs" aria-label="Складские операции">'+''.join(f'<a class="operation-tab{" active" if op==selected else ""}" id="tab-{op}" href="/transactions?op={op}" data-op="{op}" aria-current="{ "page" if op==selected else "false"}">{label}</a>' for op,label in operations)+'</nav>'
        preset=None
        try: preset=con.execute("SELECT id,code FROM products WHERE id=? AND active=1",(int(query.get("product_id",["0"])[0]),)).fetchone()
        except ValueError: pass
        payload={"op":selected,"product":dict(preset) if preset else None,"queue":[p["name"] for p in queue_items]}
        body=tabs+body+f'<script type="application/json" id="operation-preset">{script_json(payload)}</script>'
        return self.send(layout("Учёт и движение",body,user,notice,active="transactions"))

    def held_api(self,con,user,query):
        self.require(user,"transactions.manage")
        try: wid=int(query.get("worker_id",[""])[0])
        except: return self.send(json.dumps([]),content_type="application/json; charset=utf-8")
        worker=con.execute("SELECT id FROM workers WHERE id=? AND active=1",(wid,)).fetchone()
        if not worker: return self.send(json.dumps([]),content_type="application/json; charset=utf-8")
        rows=[]
        for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
            pairs=con.execute("SELECT DISTINCT machine_id FROM movements WHERE product_id=? AND worker_id=? AND machine_id IS NOT NULL AND voided_at IS NULL",(p["id"],wid)).fetchall()
            for state,state_name in STATES.items():
                for pair in pairs:
                    mid=pair[0]; qty=balance(con,p["id"],state,worker_id=wid,machine_id=mid)
                    if qty>0:
                        machine=con.execute("SELECT name FROM machines WHERE id=?",(mid,)).fetchone()
                        rows.append({"product_id":p["id"],"name":p["name"],"code":p["code"],"state":state,"state_name":state_name,"machine_id":mid,"machine":machine[0] if machine else "Станок удален","qty":int(qty)})
        return self.send(json.dumps(rows,ensure_ascii=False),content_type="application/json; charset=utf-8")

    def products_api(self,con,user,query):
        self.require(user,"transactions.manage")
        q=(query.get("q",[""])[0] or "").strip().casefold()
        mode=query.get("mode",[""])[0]
        if len(q)<2 or mode not in ("receipt","issue","writeoff"):
            return self.send("[]",content_type="application/json; charset=utf-8")
        rows=[]
        for p in con.execute("SELECT id,code,name,model,category,manufacturer,location,unit,params_json FROM products WHERE active=1 ORDER BY name"):
            if q not in " ".join(str(p[k] or "") for k in ("name","code","model","category","manufacturer")).casefold(): continue
            stocks=[]
            for state in AVAILABLE_STATES:
                qty=balance(con,p["id"],state)
                if qty>0: stocks.append({"state":state,"label":STATES[state],"qty":int(qty) if float(qty).is_integer() else qty})
            if mode=="issue" and not stocks: continue
            rows.append({"id":p["id"],"code":p["code"],"name":p["model"] or p["name"],"product_name":p["name"],"model":p["model"],"category":p["category"],"manufacturer":p["manufacturer"],"location":p["location"],"unit":p["unit"],"stocks":stocks,"regrind_variant":bool(json.loads(p["params_json"] or "{}").get("has_been_regrind"))})
            if len(rows)>=20: break
        return self.send(json.dumps(rows,ensure_ascii=False),content_type="application/json; charset=utf-8")

    def positive(self,data,key):
        try: n=float(data.get(key,""))
        except: raise ValueError("Количество должно быть числом.")
        if n<=0 or not n.is_integer(): raise ValueError("Количество должно быть целым числом больше нуля.")
        return n

    def receipt(self,con,user,data,files=None):
        begin_write(con)
        self.require(user,"transactions.manage"); qty=self.positive(data,"qty"); state="new"
        pid=0
        if data.get("product_id")=="new":
            item_class=data.get("new_item_class")
            if item_class not in ("consumable","fixture"): raise ValueError("Выберите тип новой позиции.")
            category=data.get("new_category_custom","").strip() if data.get("new_category")=="__custom__" else data.get("new_category","").strip()
            name=data.get("new_name","").strip(); unit=data.get("new_unit","").strip() or "шт"
            if not category or not name: raise ValueError("Для новой позиции укажите категорию и наименование.")
            if item_class=="consumable" and is_insert_category(category) and unit.casefold()!="шт": raise ValueError("Для сменных пластин единица учета должна быть «шт», чтобы корректно суммировать остаток.")
            try: minimum=float(data.get("new_min_qty") or 0)
            except: raise ValueError("Минимальный остаток должен быть целым числом.")
            if minimum<0 or not minimum.is_integer(): raise ValueError("Минимальный остаток должен быть целым неотрицательным числом.")
            minimum=int(minimum)
            try: price=float(data.get("new_price_per_unit") or 0)
            except: raise ValueError("Стоимость должна быть числом больше или равным нулю.")
            if not math.isfinite(price) or price<0 or not price.is_integer(): raise ValueError("Стоимость за 1 шт. должна быть целым числом рублей.")
            price=int(price)
            inventory_no=data.get("new_inventory_no","").strip() if item_class=="fixture" else None
            if item_class=="fixture" and not inventory_no: raise ValueError("Для оснастки укажите инвентарный номер.")
            params=normalized_category_params(data,"new_",category,item_class)
            groups=data.get("new_material_groups",[])
            if isinstance(groups,str): groups=[groups]
            groups=list(dict.fromkeys(str(g).strip().upper() for g in groups if str(g).strip())) if item_class=="consumable" else []
            if any(group not in ISO_513_GROUPS for group in groups): raise ValueError("Выберите группы материала по ISO 513: P, M, K, N, S или H.")
            if groups:
                params["material_groups"]=groups
                params["material_group"]=groups[0]
            model=data.get("new_model","").strip()
            if item_class=="consumable" and is_insert_category(category):
                model=insert_base_code(params) if params.get("insert_kind")!="thread" else params.get("thread_insert_designation","")
            legacy="; ".join(f'{label}: {params[key]}' for key,label in TOOL_PARAMETER_FIELDS if params.get(key))
            legacy += ("; " if legacy and (params.get("insert_designation") or params.get("thread_insert_designation")) else "") + (f'Сменная пластина: {params.get("insert_designation")}' if params.get("insert_designation") else f'Резьбовая пластина: {params.get("thread_insert_designation")}' if params.get("thread_insert_designation") else "")
            photo_name=None; photo_file=(files or {}).get("new_photo")
            if photo_file and photo_file[1]:
                _,content,mime=photo_file; ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}.get(mime)
                if not ext or len(content)>5*1024*1024: raise ValueError("Фото должно быть JPG, PNG или WebP и не больше 5 МБ.")
                photo_name=secrets.token_hex(16)+"."+ext; (UPLOAD_DIR/photo_name).write_bytes(content)
            cur=con.execute("""INSERT INTO products(code,item_class,category,name,model,manufacturer,parameters,params_json,inventory_no,unit,min_qty,price_per_unit,location,photo,created_at)
              VALUES('PENDING',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(item_class,category,name,model,data.get("new_manufacturer","").strip(),legacy,json.dumps(params,ensure_ascii=False),inventory_no,unit,minimum,price,data.get("new_location","").strip(),photo_name,now_iso()))
            pid=cur.lastrowid; con.execute("UPDATE products SET code=? WHERE id=?",(f'{"INS" if item_class=="consumable" else "OSN"}-{pid:06d}',pid))
        else:
            try: pid=int(data.get("product_id") or 0)
            except: pid=0
        if not con.execute("SELECT 1 FROM products WHERE id=? AND active=1",(pid,)).fetchone(): raise ValueError("Выберите позицию для поступления или создайте новую карточку.")
        movement(con,pid,"receipt",qty,user,state_to=state,note=data.get("note","")); return self.redirect("/transactions","Поступление записано")

    def issue(self,con,user,data):
        self.require(user,"transactions.manage")
        # Reserve the SQLite writer lock before reading stock so concurrent
        # requests cannot both pass validation against the same last units.
        begin_write(con)
        pid=int(data.get("product_id") or 0); wid=int(data.get("worker_id") or 0); qty=self.positive(data,"qty"); state=data.get("state")
        worker=con.execute("SELECT * FROM workers WHERE id=? AND active=1",(wid,)).fetchone()
        if not worker: raise ValueError("Выберите рабочего.")
        try: mid=int(data.get("machine_id") or 0)
        except: mid=0
        assigned=[r[0] for r in con.execute("SELECT machine_id FROM worker_machines WHERE worker_id=?",(wid,))]
        if not assigned and worker["machine_id"]: assigned=[worker["machine_id"]]
        if not assigned: raise ValueError("У выбранного рабочего нет закрепленного станка.")
        if not con.execute("SELECT 1 FROM products WHERE id=? AND active=1",(pid,)).fetchone(): raise ValueError("Активная карточка не найдена.")
        if not con.execute("SELECT 1 FROM machines WHERE id=? AND active=1",(mid,)).fetchone(): raise ValueError("Выберите действующий станок.")
        if mid not in assigned: raise ValueError("Выберите станок, закрепленный за рабочим.")
        if state not in AVAILABLE_STATES: raise ValueError("Выберите состояние инструмента для выдачи.")
        avail=balance(con,pid,state) if state in AVAILABLE_STATES else 0
        if avail+1e-9<qty: raise ValueError(f"Недостаточный остаток на складе в состоянии «{STATES.get(state,'?')}»: доступно {avail:g}.")
        batch=uuid.uuid4().hex; movement(con,pid,"issue",qty,user,state_from=state,state_to=state,worker_id=wid,machine_id=mid,note=data.get("note",""),batch_id=batch)
        return self.redirect("/transactions","Выдача закреплена за рабочим и станком")

    def settle(self,con,user,data):
        begin_write(con)
        self.require(user,"transactions.manage"); pid=int(data.get("product_id") or 0); wid=int(data.get("worker_id") or 0); mid=int(data.get("machine_id") or 0); state=data.get("state_from"); qty=self.positive(data,"qty")
        if state not in AVAILABLE_STATES or data.get("action") not in ("return","regrind","writeoff"): raise ValueError("Выберите позицию и действие сдачи.")
        held=balance(con,pid,state,worker_id=wid,machine_id=mid)
        if qty>held+1e-9: raise ValueError(f"За выбранным рабочим по этой позиции и станку числится {held:g} шт. Обновите страницу сдачи и выберите позицию из списка.")
        batch=uuid.uuid4().hex; note=data.get("note","")
        if data["action"]=="writeoff" and len(note.strip())<3: raise ValueError("Укажите причину списания не короче 3 символов.")
        if data["action"]=="return":
            movement(con,pid,"return",qty,user,state_from=state,state_to="used",worker_id=wid,machine_id=mid,note=note,batch_id=batch)
        elif data["action"]=="regrind":
            movement(con,pid,"return",qty,user,state_from=state,state_to="regrind_queue",worker_id=wid,machine_id=mid,note=("К отправке на переточку. "+note).strip(),batch_id=batch)
        else: movement(con,pid,"writeoff_issued",qty,user,state_from=state,worker_id=wid,machine_id=mid,note=note,batch_id=batch)
        return self.redirect("/transactions","Сдача и списание записаны в историю")

    def send_regrind(self,con,user,data):
        begin_write(con)
        self.require(user,"transactions.manage"); pid=int(data.get("product_id") or 0); qty=self.positive(data,"qty")
        avail=balance(con,pid,"regrind_queue")
        if avail+1e-9<qty: raise ValueError(f"В очереди на отправку только {avail:g} шт.")
        movement(con,pid,"regrind_send",qty,user,state_from="regrind_queue",state_to="regrind",note=data.get("note","") or "Отправлено на переточку")
        return self.redirect("/transactions","Отправка на переточку зарегистрирована")

    def send_all_regrind(self,con,user,data):
        begin_write(con)
        self.require(user,"transactions.manage")
        snapshot=[[r["id"],balance(con,r["id"],"regrind_queue")] for r in con.execute("SELECT id FROM products WHERE active=1 ORDER BY id") if balance(con,r["id"],"regrind_queue")>0]
        if data.get("queue_snapshot")!=json.dumps(snapshot,separators=(",",":")):
            raise ValueError("Состав очереди изменился. Обновите страницу и проверьте список перед отправкой.")
        batch=uuid.uuid4().hex; sent=0
        for p in con.execute("SELECT id FROM products WHERE active=1 ORDER BY id"):
            qty=balance(con,p["id"],"regrind_queue")
            if qty>0:
                movement(con,p["id"],"regrind_send",qty,user,state_from="regrind_queue",state_to="regrind",note="Массовая отправка всей очереди на переточку",batch_id=batch)
                sent+=1
        if not sent: raise ValueError("В очереди нет позиций для отправки.")
        return self.redirect("/transactions",f"На переточку отправлено позиций: {sent}")

    def receive_regrind(self,con,user,data):
        begin_write(con)
        self.require(user,"transactions.manage"); pid=int(data.get("product_id") or 0); qty=self.positive(data,"qty")
        product=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if not product: raise ValueError("Выберите инструмент из списка на переточке.")
        avail=balance(con,pid,"regrind")
        if avail+1e-9<qty: raise ValueError(f"У подрядчика числится только {avail:g} шт.")
        params=json.loads(product["params_json"] or "{}"); changes=[]
        for key,label in (("diameter","Ø"),("l1","l1")):
            raw=data.get(key,"").strip()
            if raw:
                try: value=number(raw)
                except: raise ValueError(f"Параметр {label} должен быть числом.")
                if not math.isfinite(value) or value<=0: raise ValueError(f"Параметр {label} должен быть больше нуля.")
                old=params.get(key,""); params[key]=raw
                if user["role"]!="ADMIN" and str(old).strip() and float(old)!=value:
                    raise PermissionError("Размеры карточки после переточки может изменять только администратор.")
                if user["role"]!="ADMIN" and not str(old).strip():
                    raise PermissionError("Размеры карточки после переточки может изменять только администратор.")
                if str(old)!=raw: changes.append(f"{label}: {old or 'не задано'} → {raw} мм")
        params["has_been_regrind"]=True
        params["regrind_source_code"]=product["code"]
        legacy="; ".join(f'{label}: {params[key]}' for key,label in TOOL_PARAMETER_FIELDS if params.get(key))
        inventory_no=None if product["item_class"]=="fixture" else product["inventory_no"]
        cur=con.execute("""INSERT INTO products(code,item_class,category,name,model,manufacturer,parameters,params_json,inventory_no,unit,min_qty,price_per_unit,location,photo,created_at)
          VALUES('PENDING',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(product["item_class"],product["category"],product["name"],product["model"],product["manufacturer"],legacy,json.dumps(params,ensure_ascii=False),inventory_no,product["unit"],0,product["price_per_unit"],product["location"],product["photo"],now_iso()))
        new_id=cur.lastrowid; new_code=f'{"INS" if product["item_class"]=="consumable" else "OSN"}-{new_id:06d}'
        if product["item_class"]=="fixture" and product["inventory_no"]:
            con.execute("UPDATE products SET inventory_no=? WHERE id=?",(f'{product["inventory_no"]}-R{new_id}',new_id))
        con.execute("UPDATE products SET code=? WHERE id=?",(new_code,new_id))
        note="Размеры после переточки: "+("; ".join(changes) if changes else "без изменений")
        if data.get("note"," ").strip(): note+=". "+data["note"].strip()
        batch=uuid.uuid4().hex
        movement(con,pid,"regrind_receive",qty,user,state_from="regrind",note=f"Принято с переточки и перенесено в карточку {new_code}",batch_id=batch)
        movement(con,new_id,"regrind_receive",qty,user,state_to="sharpened",note=f"Новая карточка после переточки исходной позиции {product['code']}. {note}",batch_id=batch)
        return self.redirect("/transactions",f"Создана отдельная карточка {new_code}; исходная карточка и ее характеристики сохранены")

    def writeoff(self,con,user,data):
        begin_write(con)
        self.require(user,"transactions.manage"); pid=int(data.get("product_id") or 0); qty=self.positive(data,"qty"); state=data.get("state")
        if len(data.get("note", "").strip())<3: raise ValueError("Укажите причину списания не короче 3 символов.")
        avail=balance(con,pid,state) if state in AVAILABLE_STATES else 0
        if avail+1e-9<qty: raise ValueError(f"На складе доступно только {avail:g}.")
        movement(con,pid,"writeoff_stock",qty,user,state_from=state,note=data.get("note","")); return self.redirect("/transactions","Списание со склада записано")

    def export_regrind_queue(self,con,user):
        self.require(user,"transactions.manage"); output=io.StringIO(); writer=csv.writer(output,delimiter=";")
        writer.writerow(["Код","Инструмент","Модель","Категория","Производитель","Ø, мм","l1, мм","Количество","Ед.","Стоимость за шт., руб."])
        for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
            qty=balance(con,p["id"],"regrind_queue")
            if qty:
                params=json.loads(p["params_json"] or "{}")
                writer.writerow([p["code"],p["name"],p["model"],p["category"],p["manufacturer"],params.get("diameter",""),params.get("l1",""),fmt_num(qty),p["unit"],fmt_num(p["price_per_unit"])])
        return self.send("\ufeff"+output.getvalue(),content_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=regrind_queue.csv"})

    def purchasing(self,con,user,notice=""):
        self.require(user,"purchasing.view")
        can_order=has_permission(user,"purchasing.manage")
        query=parse_qs(urlparse(self.path).query)
        view=query.get("view",["open"])[0]
        if view not in ("open","closed","archived"): view="open"
        try: page=max(1,int(query.get("page",["1"])[0]))
        except ValueError: page=1
        conditions={"open":"r.archived_at IS NULL AND r.status!='Закрыта'","closed":"r.archived_at IS NULL AND r.status='Закрыта'","archived":"r.archived_at IS NOT NULL"}
        count=con.execute("SELECT COUNT(*) FROM purchase_requests r WHERE "+conditions[view]).fetchone()[0]
        pages=max(1,math.ceil(count/20));page=min(page,pages)
        requests=con.execute("SELECT r.*,u.username,(SELECT COUNT(*) FROM purchase_request_lines l WHERE l.request_id=r.id) item_count,(SELECT COALESCE(SUM(l.qty*l.unit_price),0) FROM purchase_request_lines l WHERE l.request_id=r.id) total FROM purchase_requests r JOIN users u ON u.id=r.created_by WHERE "+conditions[view]+" ORDER BY r.id DESC LIMIT 20 OFFSET ?",((page-1)*20,)).fetchall()
        low=[]
        for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY category,name"):
            qty=usable_total(con,p["id"])
            if qty<float(p["min_qty"]) or qty==0: low.append((p,qty,max(1,math.ceil(float(p["min_qty"])-qty))))
        rows=[]
        for p,qty,suggested in low:
            rows.append(f'<tr><td><b>{h(p["name"])}</b><small>{h(p["code"])} · {h(p["category"])}</small></td><td>{h(fmt_num(qty))} {h(p["unit"])}</td><td>{h(fmt_num(p["min_qty"]))} {h(p["unit"])}</td><td>{h(fmt_num(p["price_per_unit"]))}</td><td><input class="qty-input" type="number" name="qty_{p["id"]}" min="0" step="1" value="{suggested}" {"" if can_order else "disabled"}></td></tr>')
        history=[]
        for r in requests:
            actions=f'<a class="text-link" href="/purchasing?export=1&amp;id={r["id"]}">Выгрузить CSV</a>'
            if can_order:
                if r["archived_at"]:
                    actions+=f'<form method="post" action="/purchasing/restore">{csrf(user)}<input type="hidden" name="id" value="{r["id"]}"><button class="button secondary">Вернуть в список</button></form>'
                else:
                    if r["status"]!="Закрыта":
                        actions+=f'<form method="post" action="/purchasing/close" onsubmit="return confirm(\'Закрыть выполненный заказ? Это не оформляет поступление на склад.\')">{csrf(user)}<input type="hidden" name="id" value="{r["id"]}"><button class="button primary">Закрыть заказ</button></form>'
                    actions+=f'<form method="post" action="/purchasing/archive" onsubmit="return confirm(\'Удалить заявку из списка? Она останется в архиве.\')">{csrf(user)}<input type="hidden" name="id" value="{r["id"]}"><button class="text-link danger-link">Удалить из списка</button></form>'
            closed=f'<small>Закрыта: {h(r["closed_at"].replace("T"," ")[:16])}</small>' if r["closed_at"] else ""
            status="В архиве" if r["archived_at"] else r["status"]
            history.append(f'<tr data-request-id="{r["id"]}"><td><b>{h(r["request_no"] or r["id"])}</b></td><td>{h(r["created_at"].replace("T"," ")[:16])}</td><td>{h(r["username"])}</td><td>{r["item_count"]}</td><td>{h(fmt_num(r["total"]))} руб.</td><td><span class="tag">{h(status)}</span>{closed}</td><td><div class="purchase-actions">{actions}</div></td></tr>')
        tabs='<nav class="purchase-tabs" aria-label="Статусы заявок">'+''.join(f'<a class="button {"primary" if view==key else "secondary"}" href="/purchasing?view={key}#purchase-history" {"aria-current=page" if view==key else ""}>{label}</a>' for key,label in (("open","Открытые"),("closed","Завершённые"),("archived","Архив")))+'</nav>'
        pager=f'<div class="list-pagination"><span>Страница {page} из {pages} · Заявок: {count}</span>'
        if page>1: pager+=f'<a class="button secondary" href="/purchasing?view={view}&amp;page={page-1}#purchase-history">Назад</a>'
        if page<pages: pager+=f'<a class="button secondary" href="/purchasing?view={view}&amp;page={page+1}#purchase-history">Далее</a>'
        pager+='</div>'
        body=f'''<section class="panel"><div class="section-head"><div><h2>Позиции для пополнения</h2><p>Позиции с доступным остатком ниже минимума или равным нулю. Количество по умолчанию рассчитано до минимального уровня.</p></div><a class="button secondary" href="/catalog">Каталог</a></div><form method="post" action="/purchasing/create">{csrf(user)}<div class="table-wrap"><table><thead><tr><th>Позиция</th><th>Доступно</th><th>Минимум</th><th>Цена, руб./шт.</th><th>Заказать</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=5 class=empty>Нет позиций ниже минимума</td></tr>'}</tbody></table></div><p class="form-help">Измените количество или укажите 0, чтобы исключить позицию.</p><button class="button primary" {'disabled' if not low or not can_order else ''}>Сформировать заявку на закупку</button></form></section><section class="panel" id="purchase-history"><div class="section-head"><div><h2>Сформированные заявки</h2><p>Закройте выполненный заказ или удалите его из списка в архив. Закрытие заказа не изменяет складские остатки — поступление оформляется отдельно.</p></div></div>{tabs}<div class="table-wrap"><table><thead><tr><th>Номер</th><th>Дата</th><th>Создал</th><th>Позиций</th><th>Сумма</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{''.join(history) or '<tr><td colspan=7 class=empty>Заявок в этом списке нет</td></tr>'}</tbody></table></div>{pager}</section>'''
        return self.send(layout("Заявки",body,user,notice,active="purchasing"))

    def change_purchase_request(self,con,user,data,action):
        self.require(user,"purchasing.manage");begin_write(con)
        try: request_id=int(data.get("id", "0"))
        except ValueError: raise ValueError("Укажите корректную заявку.")
        row=con.execute("SELECT * FROM purchase_requests WHERE id=?",(request_id,)).fetchone()
        if not row: raise ValueError("Заявка не найдена.")
        if action=="close":
            if row["archived_at"]: raise ValueError("Сначала верните заявку из архива.")
            if row["status"]=="Закрыта": raise ValueError("Заказ уже закрыт.")
            con.execute("UPDATE purchase_requests SET status='Закрыта',closed_at=?,closed_by=? WHERE id=?",(now_iso(),user["id"],request_id))
            return self.redirect("/purchasing?view=closed#purchase-history","Заказ закрыт")
        if action=="archive":
            if row["archived_at"]: raise ValueError("Заявка уже в архиве.")
            con.execute("UPDATE purchase_requests SET archived_at=?,archived_by=? WHERE id=?",(now_iso(),user["id"],request_id))
            return self.redirect("/purchasing#purchase-history","Заявка удалена из списка и сохранена в архиве")
        if action=="restore":
            if not row["archived_at"]: raise ValueError("Заявка не находится в архиве.")
            con.execute("UPDATE purchase_requests SET archived_at=NULL,archived_by=NULL WHERE id=?",(request_id,))
            view="closed" if row["status"]=="Закрыта" else "open"
            return self.redirect(f"/purchasing?view={view}#purchase-history","Заявка возвращена в список")
        raise ValueError("Неизвестное действие с заявкой.")

    def create_purchase_request(self,con,user,data):
        self.require(user,"purchasing.manage"); selected=[]
        for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY id"):
            raw=data.get(f'qty_{p["id"]}')
            if raw is None: continue
            try: qty=float(raw or 0)
            except: raise ValueError("Количество в заявке должно быть целым числом.")
            if qty<0 or not qty.is_integer(): raise ValueError("Количество в заявке должно быть целым неотрицательным числом.")
            if not qty: continue
            stock=usable_total(con,p["id"])
            if stock>=float(p["min_qty"]) and stock>0: raise ValueError(f"{p['name']} больше не ниже минимума. Обновите страницу заявки.")
            selected.append((p,int(qty),stock))
        if not selected: raise ValueError("Укажите количество хотя бы для одной позиции.")
        cur=con.execute("INSERT INTO purchase_requests(created_at,created_by) VALUES(?,?)",(now_iso(),user["id"]))
        request_id=cur.lastrowid; request_no=f"ZAK-{request_id:06d}"
        con.execute("UPDATE purchase_requests SET request_no=? WHERE id=?",(request_no,request_id))
        for p,qty,stock in selected:
            con.execute("INSERT INTO purchase_request_lines(request_id,product_id,code,name,category,qty,unit,unit_price,stock_at_request,min_at_request) VALUES(?,?,?,?,?,?,?,?,?,?)",(request_id,p["id"],p["code"],p["name"],p["category"],qty,p["unit"],p["price_per_unit"],stock,p["min_qty"]))
        return self.redirect("/purchasing",f"Заявка {request_no} сформирована")

    def export_purchase_request(self,con,user,query):
        self.require(user,"purchasing.view")
        try: request_id=int(query.get("id",[""])[0])
        except: raise ValueError("Укажите номер заявки.")
        request=con.execute("SELECT * FROM purchase_requests WHERE id=?",(request_id,)).fetchone()
        if not request: raise ValueError("Заявка не найдена.")
        output=io.StringIO(); writer=csv.writer(output,delimiter=";")
        writer.writerow(["Заявка",request["request_no"],"Дата",request["created_at"]]); writer.writerow(["Код","Позиция","Категория","Количество","Ед.","Остаток при заявке","Минимум","Цена за шт., руб.","Сумма, руб."])
        for line in con.execute("SELECT * FROM purchase_request_lines WHERE request_id=? ORDER BY id",(request_id,)):
            writer.writerow([line["code"],line["name"],line["category"],line["qty"],line["unit"],fmt_num(line["stock_at_request"]),fmt_num(line["min_at_request"]),fmt_num(line["unit_price"]),fmt_num(line["qty"]*line["unit_price"])])
        return self.send("\ufeff"+output.getvalue(),content_type="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename={request["request_no"]}.csv'})

    def reports(self,con,user,query,notice=""):
        machine_id=query.get("machine",[""])[0]; worker_id=query.get("worker",[""])[0]
        if user["role"]=="WORKER": worker_id=str(user["worker_id"] or "")
        products=con.execute("SELECT * FROM products WHERE active=1 ORDER BY category,name").fetchall(); rows=[]
        for p in products:
            if worker_id and machine_id: held=held_total(con,p["id"],int(worker_id),int(machine_id))
            elif worker_id: held=held_total(con,p["id"],worker_id=int(worker_id))
            elif machine_id: held=held_total(con,p["id"],machine_id=int(machine_id))
            else: held=sum(float(con.execute("SELECT COALESCE(SUM(CASE WHEN kind='issue' AND state_to=? THEN qty WHEN kind IN ('return','writeoff_issued') AND state_from=? THEN -qty ELSE 0 END),0) FROM movements WHERE product_id=? AND voided_at IS NULL",(st,st,p["id"])).fetchone()[0]) for st in STATES)
            wh=usable_total(con,p["id"])
            include_row=held>0 if worker_id else bool(wh or held)
            if include_row: rows.append(f'<tr><td><b>{h(p["name"])}</b><small>{h(p["code"])}</small></td><td>{h(p["category"])}</td><td>{h(fmt_num(wh))} {h(p["unit"])}</td><td>{h(fmt_num(held))} {h(p["unit"])}</td><td>{h(fmt_num(p["min_qty"]))} {h(p["unit"])}</td></tr>')
        options_machine=machine_select(con,machine_id)
        options_worker=worker_select(con,worker_id)
        worker_machine_map={}
        machine_worker_map={}
        for link in con.execute("SELECT wm.worker_id,wm.machine_id FROM worker_machines wm JOIN workers w ON w.id=wm.worker_id AND w.active=1 JOIN machines m ON m.id=wm.machine_id AND m.active=1"):
            worker_machine_map.setdefault(str(link["worker_id"]),[]).append(str(link["machine_id"]))
            machine_worker_map.setdefault(str(link["machine_id"]),[]).append(str(link["worker_id"]))
        worker_machine_json=script_json(worker_machine_map)
        machine_worker_json=script_json(machine_worker_map)
        body=f'''<section class="panel"><div class="section-head"><div><h2>Остатки и закрепление</h2><p>Снимок по текущему состоянию учета</p></div><a class="button secondary" href="/reports?export=balances&machine={quote(machine_id)}&worker={quote(worker_id)}">Скачать CSV</a></div><form class="filter-row" method="get" action="/reports">{select_field('Станок','machine',options_machine,machine_id,attrs='id=report-machine')}{select_field('Рабочий','worker',options_worker,worker_id,attrs='id=report-worker')}<button class="button primary">Показать</button></form><div class="table-wrap"><table><thead><tr><th>Позиция</th><th>Категория</th><th>Доступно</th><th>Закреплено</th><th>Минимум</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=5 class=empty>Нет остатков и закрепленного инструмента</td></tr>'}</tbody></table></div></section>
        <script>const workerMachines={worker_machine_json},machineWorkers={machine_worker_json},machineFilter=document.getElementById('report-machine'),workerFilter=document.getElementById('report-worker'),allMachineOptions=[...machineFilter.options];function filterWorkerMachines(){{const linked=workerMachines[workerFilter.value]||[];for(const option of allMachineOptions)option.disabled=!!workerFilter.value&&option.value!==''&&linked.length>0&&!linked.includes(option.value);machineFilter.required=!!workerFilter.value&&linked.length>1;machineFilter.options[0].text=machineFilter.required?'Выберите станок рабочего':'Все станки';}}machineFilter.addEventListener('change',()=>{{const linked=machineWorkers[machineFilter.value]||[];if(linked.length===1)workerFilter.value=linked[0];else if(workerFilter.value&&!linked.includes(workerFilter.value))workerFilter.value='';filterWorkerMachines();}});workerFilter.addEventListener('change',()=>{{const linked=workerMachines[workerFilter.value]||[];if(linked.length===1)machineFilter.value=linked[0];else if(linked.length>1&&!linked.includes(machineFilter.value))machineFilter.value='';filterWorkerMachines();}});filterWorkerMachines();</script>
        <section class="panel"><div class="section-head"><div><h2>История за период</h2><p>Фильтр по дате, станку и рабочему</p></div><a class="button secondary" href="/reports?export=history&machine={quote(machine_id)}&worker={quote(worker_id)}&from={quote(query.get('from',[''])[0])}&to={quote(query.get('to',[''])[0])}">Скачать CSV</a></div><form class="filter-row" method="get" action="/reports"><input type="hidden" name="machine" value="{h(machine_id)}"><input type="hidden" name="worker" value="{h(worker_id)}">{field('С','from',query.get('from',[''])[0],typ='date')}{field('По','to',query.get('to',[''])[0],typ='date')}<button class="button primary">Показать</button></form>{self.movement_table(con,user,query,machine_id,worker_id,limit=80)}</section>'''
        return self.send(layout("Отчёты",body,user,notice,active="reports"))

    def movement_query(self,con,user,query,machine_id="",worker_id="",limit=300):
        sql="SELECT mv.*,p.name product,p.code,w.name worker,m.name machine,u.username actor,(SELECT COUNT(*) FROM movement_changes mc WHERE mc.movement_id=mv.id AND mc.action='edit') edit_count,(SELECT reason FROM movement_changes mc WHERE mc.movement_id=mv.id ORDER BY mc.id DESC LIMIT 1) latest_change_reason FROM movements mv JOIN products p ON p.id=mv.product_id LEFT JOIN workers w ON w.id=mv.worker_id LEFT JOIN machines m ON m.id=mv.machine_id LEFT JOIN users u ON u.id=mv.actor_user_id WHERE 1=1"; args=[]
        if query.get("from",[""])[0]: sql+=" AND date(mv.occurred_at)>=date(?)"; args.append(query["from"][0])
        if query.get("to",[""])[0]: sql+=" AND date(mv.occurred_at)<=date(?)"; args.append(query["to"][0])
        if machine_id: sql+=" AND mv.machine_id=?"; args.append(int(machine_id))
        if worker_id: sql+=" AND mv.worker_id=?"; args.append(int(worker_id))
        if user["role"]=="WORKER": sql+=" AND mv.worker_id=?"; args.append(user["worker_id"] or -1)
        sql+=" ORDER BY mv.id DESC LIMIT ?"; args.append(limit)
        return con.execute(sql,args).fetchall()

    def movement_table(self,con,user,query,machine_id="",worker_id="",limit=100):
        labels={"receipt":"Поступление","issue":"Выдача","return":"Возврат","writeoff_stock":"Списание со склада","writeoff_issued":"Списание у рабочего","regrind_send":"Отправка на переточку","regrind_receive":"Прием после переточки"}
        rows=[]
        for r in self.movement_query(con,user,query,machine_id,worker_id,limit):
            states=(STATES.get(r["state_from"],"") if r["state_from"] else "")
            if r["kind"] in ("return","regrind_send","regrind_receive"): states+=f' → {STATES.get(r["state_to"],"")}'
            if r["voided_at"]: actions='<span class="tag">Отменена</span>'
            elif not has_permission(user,"transactions.manage"): actions="—"
            elif not within_edit_window(r["occurred_at"]): actions='<small>Срок истёк</small>'
            else:
                actions=f'''<details class="movement-edit"><summary>Исправить</summary>{'<p class="form-help">Для приёмки после переточки изменятся обе связанные записи.</p>' if r["kind"]=="regrind_receive" else ""}<form method="post" action="/movement/edit">{csrf(user)}<input type="hidden" name="id" value="{r["id"]}"><label>Количество, шт<input type="number" name="qty" min="1" step="1" required value="{h(fmt_num(r["qty"]))}"></label><label>Комментарий<input name="note" value="{h(r["note"])}"></label><label>Причина исправления<input name="reason" required minlength="3"></label><button class="button secondary">Сохранить исправление</button></form><form method="post" action="/movement/cancel" onsubmit="return confirm(\'Отменить операцию? Остатки будут пересчитаны.\')">{csrf(user)}<input type="hidden" name="id" value="{r["id"]}"><label>Причина отмены<input name="reason" required minlength="3"></label><button class="text-link danger-link">Отменить операцию</button></form></details>'''
            status="Отменена" if r["voided_at"] else (f'Исправлена {r["edit_count"]} раз' if r["edit_count"] else "")
            reason=r["void_reason"] if r["voided_at"] else (r["latest_change_reason"] if r["edit_count"] else r["note"])
            rows.append(f'<tr><td>{h(r["occurred_at"].replace("T"," ")[:16])}</td><td><span class="tag">{h(movement_label(r["kind"],r["state_to"]))}</span></td><td>{h(r["product"])}<small>{h(r["code"])}{" · "+h(status) if status else ""}</small></td><td>{h(fmt_num(r["qty"]))}</td><td>{h(states)}</td><td>{h(r["worker"] or "—")}</td><td>{h(r["machine"] or "—")}</td><td>{h(r["actor"] or "—")}</td><td>{h(reason)}</td><td>{actions}</td></tr>')
        return f'<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Операция</th><th>Позиция</th><th>Кол-во</th><th>Состояние</th><th>Рабочий</th><th>Станок</th><th>Кто провел</th><th>Комментарий</th><th>Исправление</th></tr></thead><tbody>{"".join(rows) or "<tr><td colspan=10 class=empty>За выбранный период операций нет</td></tr>"}</tbody></table></div>'

    def movement_edit_target(self,con,data):
        try: mid=int(data.get("id") or 0)
        except: raise ValueError("Укажите корректную операцию.")
        row=con.execute("SELECT * FROM movements WHERE id=?",(mid,)).fetchone()
        if not row: raise ValueError("Операция не найдена.")
        if row["voided_at"]: raise ValueError("Операция уже отменена.")
        if not within_edit_window(row["occurred_at"]): raise ValueError("Срок исправления операции — 24 часа с момента проведения.")
        return row

    def linked_movements(self,con,row):
        if row["kind"]!="regrind_receive": return [row]
        rows=con.execute("SELECT * FROM movements WHERE batch_id=? AND kind='regrind_receive' ORDER BY id",(row["batch_id"],)).fetchall()
        if len(rows)!=2 or any(r["voided_at"] or not within_edit_window(r["occurred_at"]) for r in rows):
            raise ValueError("Связанная приёмка неполная, отменена или недоступна для исправления. Нужна сверка истории.")
        if sum(r["state_from"]=="regrind" and not r["state_to"] for r in rows)!=1 or sum(not r["state_from"] and r["state_to"]=="sharpened" for r in rows)!=1:
            raise ValueError("Нарушена структура связанной приёмки. Нужна сверка истории.")
        if len({r["qty"] for r in rows})!=1:
            raise ValueError("Количество в связанных движениях не совпадает. Нужна сверка истории.")
        return rows

    def change_movement(self,con,user,data,action):
        self.require(user,"transactions.manage"); begin_write(con)
        row=self.movement_edit_target(con,data)
        rows=self.linked_movements(con,row)
        reason=data.get("reason","").strip()
        if len(reason)<3: raise ValueError("Укажите причину не короче 3 символов.")
        qty=self.positive(data,"qty") if action=="edit" else None
        stamp=now_iso()
        con.execute("SAVEPOINT movement_change")
        try:
            for linked in rows:
                before={k:linked[k] for k in ("kind","qty","state_from","state_to","worker_id","machine_id","note")}
                if action=="edit":
                    note=data.get("note","").strip() if linked["id"]==row["id"] else linked["note"]
                    con.execute("UPDATE movements SET qty=?,note=? WHERE id=?",(qty,note,linked["id"]))
                    after={**before,"qty":qty,"note":note}
                else:
                    con.execute("UPDATE movements SET voided_at=?,voided_by_user_id=?,void_reason=? WHERE id=?",(stamp,user["id"],reason,linked["id"]))
                    after={"voided":True}
                con.execute("INSERT INTO movement_changes(movement_id,changed_at,actor_user_id,action,reason,before_json,after_json) VALUES(?,?,?,?,?,?,?)",(linked["id"],stamp,user["id"],action,reason,json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False)))
            for pid in {r["product_id"] for r in rows}:
                if ledger_errors(con,pid): raise ValueError("Изменение нарушит остатки в истории операций. Все связанные изменения отменены.")
            con.execute("RELEASE movement_change")
        except Exception:
            con.execute("ROLLBACK TO movement_change"); con.execute("RELEASE movement_change"); raise
        return self.redirect("/history","Операция и связанные движения обновлены; изменение записано в журнал аудита")

    def edit_movement(self,con,user,data):
        return self.change_movement(con,user,data,"edit")

    def cancel_movement(self,con,user,data):
        return self.change_movement(con,user,data,"cancel")

    def history(self,con,user,query,notice=""):
        if not (has_permission(user,"history.view") or has_permission(user,"transactions.manage")): raise PermissionError("Недостаточно прав для этой операции.")
        body=f'<section class="panel"><div class="section-head"><div><h2>Журнал движений</h2><p>Все операции остаются в журнале с датой, исполнителем, рабочим и станком.</p></div><a class="button secondary" href="/reports?export=history">Скачать CSV</a></div><form class="filter-row" method="get">{field("С","from",query.get("from",[""])[0],typ="date")}{field("По","to",query.get("to",[""])[0],typ="date")}<button class="button primary">Фильтр</button></form>{self.movement_table(con,user,query,limit=300)}</section>'
        return self.send(layout("История операций",body,user,notice,active="history"))

    def export_history(self,con,user,query):
        self.require(user,"reports.view")
        machine=query.get("machine",[""])[0]; worker=query.get("worker",[""])[0]
        output=io.StringIO(); writer=csv.writer(output,delimiter=";"); writer.writerow(["Дата","Операция","Статус","Код","Позиция","Количество","Состояние от","Состояние в","Рабочий","Станок","Кто провел","Комментарий"])
        labels={"receipt":"Поступление","issue":"Выдача","return":"Возврат","writeoff_stock":"Списание со склада","writeoff_issued":"Списание у рабочего","regrind_send":"Отправка на переточку","regrind_receive":"Прием после переточки"}
        for r in self.movement_query(con,user,query,machine,worker,100000): writer.writerow([r["occurred_at"],movement_label(r["kind"],r["state_to"]),"Отменена: "+r["void_reason"] if r["voided_at"] else "Активна",r["code"],r["product"],fmt_num(r["qty"]),STATES.get(r["state_from"],""),STATES.get(r["state_to"],""),r["worker"],r["machine"],r["actor"],r["void_reason"] if r["voided_at"] else r["note"]])
        return self.send("\ufeff"+output.getvalue(),content_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=tool_movements.csv"})

    def export_balances(self,con,user,query):
        self.require(user,"reports.view")
        machine=query.get("machine",[""])[0]; worker=query.get("worker",[""])[0]
        if user["role"]=="WORKER": worker=str(user["worker_id"] or "")
        output=io.StringIO(); writer=csv.writer(output,delimiter=";"); writer.writerow(["Код","Позиция","Тип","Категория","Состояние","На складе","Закреплено","Единица","Рабочий","Станок"])
        for p in con.execute("SELECT * FROM products WHERE active=1 ORDER BY category,name"):
            for st,label in STATES.items():
                wh=balance(con,p["id"],st)
                if worker and machine: held=balance(con,p["id"],st,worker_id=int(worker),machine_id=int(machine))
                elif worker: held=balance(con,p["id"],st,worker_id=int(worker))
                elif machine: held=balance(con,p["id"],st,machine_id=int(machine))
                else: held=con.execute("SELECT COALESCE(SUM(CASE WHEN kind='issue' AND state_to=? THEN qty WHEN kind IN ('return','writeoff_issued') AND state_from=? THEN -qty ELSE 0 END),0) FROM movements WHERE product_id=? AND voided_at IS NULL",(st,st,p["id"])).fetchone()[0]
                if (held>0 if worker else (wh or held)): writer.writerow([p["code"],p["name"],"Инструмент" if p["item_class"]=="consumable" else "Оснастка",p["category"],label,fmt_num(wh),fmt_num(held),p["unit"],worker,machine])
        return self.send("\ufeff"+output.getvalue(),content_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=tool_balances.csv"})

    def archive_product(self,con,user,data):
        begin_write(con)
        self.require(user,"catalog.edit"); pid=int(data.get("id") or 0)
        p=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if not p: raise ValueError("Активная карточка не найдена.")
        warehouse=warehouse_total(con,pid)
        held=0
        for r in con.execute("SELECT DISTINCT worker_id,machine_id FROM movements WHERE product_id=? AND worker_id IS NOT NULL AND voided_at IS NULL",(pid,)):
            held+=sum(balance(con,pid,state,worker_id=r["worker_id"],machine_id=r["machine_id"]) for state in STATES)
        if warehouse>0 or held>0:
            raise ValueError(f"Карточку можно архивировать только при нулевом остатке на складе и у рабочих. Сейчас на складе {warehouse:g}, у рабочих {held:g}.")
        con.execute("UPDATE products SET active=0 WHERE id=?",(pid,))
        return self.redirect("/catalog","Карточка перемещена в архив")

    def restore_product(self,con,user,data):
        self.require(user,"catalog.edit"); pid=int(data.get("id") or 0)
        if not con.execute("SELECT 1 FROM products WHERE id=? AND active=0",(pid,)).fetchone(): raise ValueError("Карточка архива не найдена.")
        con.execute("UPDATE products SET active=1 WHERE id=?",(pid,))
        return self.redirect("/catalog?archive=1","Карточка восстановлена в каталог")

    def edit_role(self,con,user,data):
        self.require(user,("ADMIN",)); code=data.get("code","")
        role=con.execute("SELECT * FROM role_definitions WHERE code=?",(code,)).fetchone()
        if not role: raise ValueError("Роль не найдена.")
        if code=="ADMIN": raise ValueError("Права администратора всегда включают полный доступ.")
        permissions=data.get("permissions",[])
        if isinstance(permissions,str): permissions=[permissions]
        permissions=set(x for x in permissions if x in PERMISSIONS)
        if "catalog.edit" in permissions: permissions.add("catalog.view")
        if "purchasing.manage" in permissions: permissions.add("purchasing.view")
        con.execute("UPDATE role_definitions SET permissions_json=? WHERE code=?",(json.dumps(sorted(permissions),ensure_ascii=False),code))
        return self.redirect("/admin?section=roles","Права роли сохранены")

    def delete_role(self,con,user,data):
        self.require(user,("ADMIN",)); code=data.get("code","")
        role=con.execute("SELECT * FROM role_definitions WHERE code=?",(code,)).fetchone()
        if not role: raise ValueError("Роль не найдена.")
        if role["is_system"]: raise ValueError("Встроенные роли нельзя удалить.")
        count=con.execute("SELECT COUNT(*) FROM users WHERE role=? AND active=1",(code,)).fetchone()[0]
        if count: raise ValueError(f"Роль назначена {count} активным учетным записям. Сначала переназначьте их.")
        con.execute("DELETE FROM role_definitions WHERE code=?",(code,))
        return self.redirect("/admin?section=roles","Роль удалена")

    def delete_user(self,con,user,data):
        self.require(user,("ADMIN",)); uid=int(data.get("id") or 0)
        target=con.execute("SELECT * FROM users WHERE id=? AND active=1",(uid,)).fetchone()
        if not target: raise ValueError("Активная учетная запись не найдена.")
        if target["role"]=="ADMIN" and con.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='ADMIN'").fetchone()[0]<=1:
            raise ValueError("Нельзя отключить последнюю активную учетную запись администратора.")
        con.execute("UPDATE users SET active=0 WHERE id=?",(uid,)); con.execute("DELETE FROM sessions WHERE user_id=?",(uid,))
        return self.redirect("/admin?section=users","Учетная запись удалена и отключена")

    def delete_machine(self,con,user,data):
        self.require(user,("ADMIN",)); mid=int(data.get("id") or 0)
        if not con.execute("SELECT 1 FROM machines WHERE id=? AND active=1",(mid,)).fetchone(): raise ValueError("Активный станок не найден.")
        con.execute("UPDATE machines SET active=0 WHERE id=?",(mid,))
        return self.redirect("/admin?section=machines","Станок удален из справочника; записи истории сохранены")

    def delete_worker(self,con,user,data):
        self.require(user,("ADMIN",)); wid=int(data.get("id") or 0)
        if not con.execute("SELECT 1 FROM workers WHERE id=? AND active=1",(wid,)).fetchone(): raise ValueError("Активный рабочий не найден.")
        held=sum(held_total(con,p["id"],worker_id=wid) for p in con.execute("SELECT id FROM products"))
        if held>0:
            raise ValueError("Нельзя удалить: числится инструмент")
        ids=[r[0] for r in con.execute("SELECT id FROM users WHERE worker_id=? AND active=1",(wid,))]
        con.execute("UPDATE workers SET active=0 WHERE id=?",(wid,)); con.execute("UPDATE users SET active=0 WHERE worker_id=?",(wid,)); con.executemany("DELETE FROM sessions WHERE user_id=?",[(x,) for x in ids])
        return self.redirect("/admin?section=workers","Рабочий и связанные учетные записи отключены; история сохранена")

    def restore_user(self,con,user,data):
        self.require(user,("ADMIN",)); uid=int(data.get("id") or 0)
        target=con.execute("SELECT * FROM users WHERE id=? AND active=0",(uid,)).fetchone()
        if not target: raise ValueError("Отключенная учетная запись не найдена.")
        if not con.execute("SELECT 1 FROM role_definitions WHERE code=?",(target["role"],)).fetchone():
            raise ValueError("Роль этой учетной записи удалена. Назначьте существующую роль перед восстановлением.")
        if target["worker_id"] and not con.execute("SELECT 1 FROM workers WHERE id=? AND active=1",(target["worker_id"],)).fetchone():
            raise ValueError("Сначала восстановите связанного рабочего.")
        con.execute("UPDATE users SET active=1 WHERE id=?",(uid,))
        return self.redirect("/admin?section=users","Учетная запись восстановлена")

    def restore_machine(self,con,user,data):
        self.require(user,("ADMIN",)); mid=int(data.get("id") or 0)
        if not con.execute("SELECT 1 FROM machines WHERE id=? AND active=0",(mid,)).fetchone(): raise ValueError("Отключенный станок не найден.")
        con.execute("UPDATE machines SET active=1 WHERE id=?",(mid,))
        return self.redirect("/admin?section=machines","Станок восстановлен")

    def restore_worker(self,con,user,data):
        self.require(user,("ADMIN",)); wid=int(data.get("id") or 0)
        if not con.execute("SELECT 1 FROM workers WHERE id=? AND active=0",(wid,)).fetchone(): raise ValueError("Отключенный рабочий не найден.")
        con.execute("UPDATE workers SET active=1 WHERE id=?",(wid,))
        return self.redirect("/admin?section=workers","Рабочий восстановлен. Связанную учетную запись восстановите отдельно.")

    def admin(self,con,user,notice=""):
        self.require(user,("ADMIN",))
        machines=con.execute("SELECT * FROM machines WHERE active=1 ORDER BY name").fetchall()
        archived_machines=con.execute("SELECT * FROM machines WHERE active=0 ORDER BY name").fetchall()
        workers=[dict(r) for r in con.execute("SELECT w.*,m.name machine FROM workers w LEFT JOIN machines m ON m.id=w.machine_id WHERE w.active=1 ORDER BY w.name").fetchall()]
        archived_workers=con.execute("SELECT * FROM workers WHERE active=0 ORDER BY name").fetchall()
        users=con.execute("SELECT u.*,w.name worker FROM users u LEFT JOIN workers w ON w.id=u.worker_id ORDER BY u.username").fetchall()
        for w in workers:
            w["machine_list"]=", ".join(r[0] for r in con.execute("SELECT m.name FROM worker_machines wm JOIN machines m ON m.id=wm.machine_id WHERE wm.worker_id=? AND m.active=1 ORDER BY m.name",(w["id"],))) or "станок не назначен"
        role_rows=con.execute("SELECT * FROM role_definitions ORDER BY is_system DESC,name").fetchall()
        role_options="".join(f'<option value="{h(r["code"])}">{h(r["name"])}</option>' for r in role_rows)
        permission_boxes="".join(f'<label class="permission-choice"><input type="checkbox" name="permissions" value="{h(code)}"><span>{h(label)}</span></label>' for code,label in PERMISSIONS.items())
        headers={"dashboard":"Обзор","catalog.view":"Каталог","catalog.edit":"Карточки","transactions.manage":"Операции","reports.view":"Отчеты","history.view":"История","purchasing.view":"Заявки","purchasing.manage":"Заявки +"}
        roles_table=[]
        for r in role_rows:
            selected=set(json.loads(r["permissions_json"] or "[]")); locked=r["code"]=="ADMIN"
            rid="role-"+re.sub("[^a-zA-Z0-9]","",r["code"])
            form=f'<form id="{rid}" method="post" action="/admin/role/edit">{csrf(user)}<input type="hidden" name="code" value="{h(r["code"])}"></form>'
            cells="".join(f'<td><input type="checkbox" aria-label="{h(PERMISSIONS[k])}" name="permissions" value="{h(k)}" form="{rid}"{" checked" if k in selected or locked else ""}{" disabled" if locked else ""}></td>' for k in PERMISSIONS)
            delete=(f'<form method="post" action="/admin/role/delete" class="inline-action" onsubmit="return confirm(\'Удалить роль?\')">{csrf(user)}<input type="hidden" name="code" value="{h(r["code"])}"><button class="icon-button danger" aria-label="Удалить" title="Удалить">{trash_icon()}</button></form>' if not r["is_system"] else "")
            save=f'<button class="button secondary" form="{rid}"{" disabled title=\"Полный доступ администратора закреплен\"" if locked else ""}>Сохранить</button>{delete}'
            roles_table.append(f'<tr><td>{form}<b>{h(r["name"])}</b><small>{"Системная" if r["is_system"] else "Пользовательская"}</small></td>{cells}<td class="role-actions">{save}</td></tr>')
        machine_rows=[]
        for m in machines:
            mid=m["id"]
            machine_rows.append(f'<div class="directory-row"><div class="directory-content"><b>{h(m["name"])}</b><small>{h(m["location"]) or "место не указано"}</small><details class="directory-edit"><summary>Редактировать</summary><form method="post" action="/admin/machine/edit">{csrf(user)}<input type="hidden" name="id" value="{mid}">{field("Название станка","name",m["name"],required=True)}{field("Участок / расположение","location",m["location"])}<button class="button primary">Сохранить станок</button></form></details></div><form method="post" action="/admin/machine/delete" onsubmit="return confirm(\'Удалить станок из справочника? История сохранится.\')">{csrf(user)}<input type="hidden" name="id" value="{mid}"><button class="icon-button danger" aria-label="Удалить" title="Удалить">{trash_icon()}</button></form></div>')
        machine_rows="".join(machine_rows)
        archived_machine_rows="".join(f'<div class="directory-row"><div class="directory-content"><b>{h(m["name"])}</b><small>{h(m["location"]) or "место не указано"}</small></div><form method="post" action="/admin/machine/restore">{csrf(user)}<input type="hidden" name="id" value="{m["id"]}"><button class="button secondary">Восстановить</button></form></div>' for m in archived_machines)
        worker_rows=[]
        for w in workers:
            selected=[r[0] for r in con.execute("SELECT machine_id FROM worker_machines WHERE worker_id=?",(w["id"],))]
            inactive_links=[r[0] for r in con.execute("SELECT m.name FROM worker_machines wm JOIN machines m ON m.id=wm.machine_id WHERE wm.worker_id=? AND m.active=0 ORDER BY m.name",(w["id"],))]
            inactive_note=f'<small>Связанные отключенные станки сохраняются: {h(", ".join(inactive_links))}</small>' if inactive_links else ''
            wid=w["id"]
            held=sum(held_total(con,p["id"],worker_id=wid) for p in con.execute("SELECT id FROM products"))
            delete_button=f'<button class="icon-button danger" aria-label="Удалить" title="Нельзя удалить: числится инструмент" disabled>{trash_icon()}</button>' if held else f'<button class="icon-button danger" aria-label="Удалить" title="Удалить">{trash_icon()}</button>'
            held_note='<small class="warning-text">Нельзя удалить: числится инструмент</small>' if held else ''
            worker_rows.append(f'<div class="directory-row"><div class="directory-content"><b>{h(w["name"])}</b><small>{h(w["machine_list"])} · таб. № {h(w["personnel_no"] or "—")}</small>{held_note}<details class="directory-edit"><summary>Редактировать</summary><form method="post" action="/admin/worker/edit">{csrf(user)}<input type="hidden" name="id" value="{wid}">{field("ФИО / имя","name",w["name"],required=True)}{field("Табельный номер","personnel_no",w["personnel_no"] or "")}<label class="field"><span>Закрепленные станки</span></label>{machine_checkboxes(machines,selected,prefix=f"worker-{wid}")}{inactive_note}<button class="button primary">Сохранить рабочего</button></form></details></div><form method="post" action="/admin/worker/delete" onsubmit="return confirm(\'Удалить рабочего и отключить связанную учетную запись? История сохранится.\')">{csrf(user)}<input type="hidden" name="id" value="{wid}">{delete_button}</form></div>')
        worker_rows="".join(worker_rows)
        archived_worker_rows="".join(f'<div class="directory-row"><div class="directory-content"><b>{h(w["name"])}</b><small>Таб. № {h(w["personnel_no"] or "—")}</small></div><form method="post" action="/admin/worker/restore">{csrf(user)}<input type="hidden" name="id" value="{w["id"]}"><button class="button secondary">Восстановить</button></form></div>' for w in archived_workers)
        account_rows=[]
        for u in users:
            role=con.execute("SELECT name FROM role_definitions WHERE code=?",(u["role"],)).fetchone()
            delete=(f'<form method="post" action="/admin/user/delete" onsubmit="return confirm(\'Отключить учетную запись?\')">{csrf(user)}<input type="hidden" name="id" value="{u["id"]}"><button class="icon-button danger" aria-label="Удалить" title="Удалить">{trash_icon()}</button></form>' if u["active"] and u["id"]!=user["id"] else "")
            restore=(f'<form method="post" action="/admin/user/restore">{csrf(user)}<input type="hidden" name="id" value="{u["id"]}"><button class="button secondary">Восстановить</button></form>' if not u["active"] else "")
            account_rows.append(f'<tr><td>{h(u["username"])}</td><td>{h(role["name"] if role else "Роль удалена")}</td><td>{h(u["worker"] or "—")}</td><td>{h(u["created_at"][:10])}</td><td>{"Активна" if u["active"] else "Отключена"}</td><td>{delete or restore}</td></tr>')
        body=f'''<div class="two-col"><section class="panel"><div class="section-head"><div><span class="eyebrow">СПРАВОЧНИК</span><h2>Добавить станок</h2></div></div><form method="post" action="/admin/machine">{csrf(user)}{field('Название станка','name',required=True)}{field('Участок / расположение','location')}<button class="button primary">Добавить станок</button></form><h3>Станки</h3><div class="simple-list directory-list">{machine_rows or '<p class=empty>Станков пока нет</p>'}</div>{'<h3>Отключенные станки</h3><div class="simple-list directory-list">'+archived_machine_rows+'</div>' if archived_machines else ''}</section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">СПРАВОЧНИК</span><h2>Добавить рабочего</h2></div></div><form method="post" action="/admin/worker">{csrf(user)}{field('ФИО / имя','name',required=True)}{field('Табельный номер','personnel_no')}<label class="field"><span>Закрепить за станками</span></label>{machine_checkboxes(machines,prefix='new-worker')}<p class="form-help">Отметьте один или несколько станков галочками.</p><button class="button primary">Добавить рабочего</button></form><h3>Рабочие</h3><div class="simple-list directory-list">{worker_rows or '<p class=empty>Рабочих пока нет</p>'}</div>{'<h3>Отключенные рабочие</h3><div class="simple-list directory-list">'+archived_worker_rows+'</div>' if archived_workers else ''}</section></div>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">РОЛИ И ПРАВА</span><h2>Матрица доступа</h2><p>Измените разрешения и сохраните строку роли. Управление ролями доступно только администратору.</p></div></div><div class="table-wrap role-matrix"><table><thead><tr><th>Роль</th>{''.join(f'<th title="{h(PERMISSIONS[k])}">{h(headers[k])}</th>' for k in PERMISSIONS)}<th>Действия</th></tr></thead><tbody>{''.join(roles_table)}</tbody></table></div><h3>Создать роль</h3><form method="post" action="/admin/role">{csrf(user)}{field('Название роли','name',required=True)}<div class="permission-grid">{permission_boxes}</div><button class="button primary">Создать роль</button></form></section>
        <section class="panel"><div class="section-head"><div><span class="eyebrow">УЧЕТНЫЕ ЗАПИСИ</span><h2>Создать пользователя</h2><p>Для роли «Рабочий» свяжите пользователя с карточкой рабочего.</p></div></div><form method="post" action="/admin/user" class="filter-row">{csrf(user)}{field('Логин','username',required=True)}{field('Пароль (10+ символов)','password',typ='password',attrs='minlength=10',required=True)}{select_field('Роль','role',role_options)}{select_field('Рабочий','worker_id',worker_select(con))}<button class="button primary">Создать</button></form><div class="table-wrap"><table><thead><tr><th>Логин</th><th>Роль</th><th>Рабочий</th><th>Создан</th><th>Статус</th><th></th></tr></thead><tbody>{''.join(account_rows)}</tbody></table></div></section>'''
        sections=[("machines","Станки"),("workers","Рабочие"),("roles","Роли и права"),("users","Учётные записи")]
        query=parse_qs(urlparse(self.path).query)
        selected=query.get("section",["machines"])[0]
        if selected not in dict(sections): selected="machines"
        index=iter(sections)
        def directory_section(match):
            key,label=next(index)
            return f'<section class="panel admin-section" data-admin-section="{key}" id="admin-{key}" aria-labelledby="admin-tab-{key}"{" hidden" if key!=selected else ""}>'
        body=re.sub(r'<section class="panel">',directory_section,body)
        body=body.replace('<div class="two-col">','<div class="admin-directory-grid">',1)
        body=re.sub(r'<div class="simple-list directory-list">', '<label class="field directory-filter"><span>Поиск по списку</span><input type="search" data-directory-search placeholder="Имя, название, табельный номер или станок" autocomplete="off"></label><div class="simple-list directory-list" tabindex="0" aria-label="Записи справочника">',body)
        tabs='<nav class="admin-tabs" aria-label="Разделы справочников">'+''.join(f'<a class="button {"primary" if key==selected else "secondary"}" id="admin-tab-{key}" data-admin-tab="{key}" href="/admin?section={key}" {"aria-current=page" if key==selected else ""}>{label}</a>' for key,label in sections)+'</nav>'
        return self.send(layout("Справочники и доступ",tabs+body,user,notice,active="admin"))

    def add_machine(self,con,user,data):
        self.require(user,("ADMIN",)); name=data.get("name","").strip()
        if len(name)<2: raise ValueError("Укажите название станка.")
        con.execute("INSERT INTO machines(name,location) VALUES(?,?)",(name,data.get("location","").strip())); return self.redirect("/admin?section=machines","Станок добавлен")

    def edit_machine(self,con,user,data):
        self.require(user,("ADMIN",)); mid=int(data.get("id") or 0); name=data.get("name","").strip()
        if not con.execute("SELECT 1 FROM machines WHERE id=? AND active=1",(mid,)).fetchone(): raise ValueError("Активный станок не найден.")
        if len(name)<2: raise ValueError("Укажите название станка.")
        con.execute("UPDATE machines SET name=?,location=? WHERE id=?",(name,data.get("location","").strip(),mid))
        return self.redirect("/admin?section=machines","Данные станка обновлены")

    def add_worker(self,con,user,data):
        self.require(user,("ADMIN",)); name=data.get("name","").strip(); pn=data.get("personnel_no","").strip(); mids=data.get("machine_ids",[])
        if isinstance(mids,str): mids=[mids] if mids else []
        mids=list(dict.fromkeys(int(x) for x in mids if x))
        active_ids={r[0] for r in con.execute("SELECT id FROM machines WHERE active=1")}
        if any(mid not in active_ids for mid in mids): raise ValueError("Выберите только действующие станки.")
        mid=mids[0] if mids else None
        pn=pn or None
        if len(name)<2: raise ValueError("Укажите имя рабочего.")
        cur=con.execute("INSERT INTO workers(name,personnel_no,machine_id) VALUES(?,?,?)",(name,pn,mid))
        con.executemany("INSERT OR IGNORE INTO worker_machines(worker_id,machine_id) VALUES(?,?)",[(cur.lastrowid,x) for x in mids])
        return self.redirect("/admin?section=workers","Рабочий добавлен")

    def edit_worker(self,con,user,data):
        self.require(user,("ADMIN",)); wid=int(data.get("id") or 0); name=data.get("name","").strip()
        worker=con.execute("SELECT id FROM workers WHERE id=? AND active=1",(wid,)).fetchone()
        if not worker: raise ValueError("Активный рабочий не найден.")
        if len(name)<2: raise ValueError("Укажите имя рабочего.")
        pn=data.get("personnel_no","").strip() or None
        mids=data.get("machine_ids",[])
        if isinstance(mids,str): mids=[mids] if mids else []
        mids=list(dict.fromkeys(int(x) for x in mids if x))
        active_ids={r[0] for r in con.execute("SELECT id FROM machines WHERE active=1")}
        if any(mid not in active_ids for mid in mids): raise ValueError("Выберите только действующие станки.")
        inactive_ids=[r[0] for r in con.execute("SELECT wm.machine_id FROM worker_machines wm JOIN machines m ON m.id=wm.machine_id WHERE wm.worker_id=? AND m.active=0",(wid,))]
        con.execute("UPDATE workers SET name=?,personnel_no=?,machine_id=? WHERE id=?",(name,pn,mids[0] if mids else None,wid))
        con.execute("DELETE FROM worker_machines WHERE worker_id=?",(wid,))
        con.executemany("INSERT INTO worker_machines(worker_id,machine_id) VALUES(?,?)",[(wid,mid) for mid in dict.fromkeys(mids+inactive_ids)])
        return self.redirect("/admin?section=workers","Данные рабочего обновлены")

    def add_role(self,con,user,data):
        self.require(user,("ADMIN",)); name=data.get("name","").strip()
        permissions=data.get("permissions",[])
        if isinstance(permissions,str): permissions=[permissions]
        permissions=set(x for x in permissions if x in PERMISSIONS)
        if "catalog.edit" in permissions: permissions.add("catalog.view")
        if "purchasing.manage" in permissions: permissions.add("purchasing.view")
        permissions=sorted(permissions)
        if len(name)<2: raise ValueError("Укажите название роли длиной не менее двух символов.")
        if not permissions: raise ValueError("Выберите хотя бы одно разрешение для роли.")
        if con.execute("SELECT 1 FROM role_definitions WHERE name=?",(name,)).fetchone():
            raise ValueError("Роль с таким названием уже существует.")
        code="CUSTOM_"+uuid.uuid4().hex[:12].upper()
        con.execute("INSERT INTO role_definitions(code,name,permissions_json,is_system) VALUES(?,?,?,0)",(code,name,json.dumps(permissions,ensure_ascii=False)))
        return self.redirect("/admin?section=roles","Роль создана")

    def add_user(self,con,user,data):
        self.require(user,("ADMIN",)); username=data.get("username","").strip(); password=data.get("password",""); role=data.get("role")
        wid=int(data["worker_id"]) if data.get("worker_id") else None
        if len(username)<3 or len(password)<10: raise ValueError("Логин должен содержать 3 символа, пароль — не менее 10.")
        if not con.execute("SELECT 1 FROM role_definitions WHERE code=?",(role,)).fetchone(): raise ValueError("Недопустимая роль.")
        if role=="WORKER" and not wid: raise ValueError("Свяжите учетную запись рабочего с карточкой рабочего.")
        if role!="WORKER": wid=None
        con.execute("INSERT INTO users(username,password_hash,role,worker_id,created_at) VALUES(?,?,?,?,?)",(username,pw_hash(password),role,wid,now_iso())); return self.redirect("/admin?section=users","Пользователь создан")

    def page_error(self,user,status,message):
        return self.send(layout("Сообщение",f'<section class="panel error-panel"><h2>{h(message)}</h2><a class="button secondary" href="/dashboard">На главную</a></section>',user),status=status)


def main():
    init_db()
    print(f"КАТЕКС: http://127.0.0.1:{PORT}  (listen {HOST}:{PORT})")
    server=ThreadingHTTPServer((HOST,PORT),App)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nОстановка сервера")
    finally: server.server_close()


if __name__=="__main__": main()
