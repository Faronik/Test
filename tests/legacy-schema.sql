CREATE TABLE machines (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, location TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE movement_changes (
 id INTEGER PRIMARY KEY, movement_id INTEGER NOT NULL REFERENCES movements(id), changed_at TEXT NOT NULL,
 actor_user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, reason TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL
);
CREATE TABLE movements (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, occurred_at TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
 kind TEXT NOT NULL CHECK(kind IN ('receipt','issue','return','writeoff_stock','writeoff_issued','regrind_send','regrind_receive')),
 qty REAL NOT NULL CHECK(qty > 0), state_from TEXT, state_to TEXT,
 worker_id INTEGER REFERENCES workers(id), machine_id INTEGER REFERENCES machines(id),
 actor_user_id INTEGER REFERENCES users(id), note TEXT NOT NULL DEFAULT '',
 voided_at TEXT, voided_by_user_id INTEGER REFERENCES users(id), void_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE products (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, item_class TEXT NOT NULL CHECK(item_class IN ('consumable','fixture')),
 category TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', manufacturer TEXT NOT NULL DEFAULT '',
 parameters TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'шт', min_qty REAL NOT NULL DEFAULT 0,
 inventory_no TEXT, params_json TEXT NOT NULL DEFAULT '{}', price_per_unit REAL NOT NULL DEFAULT 0,
 location TEXT NOT NULL DEFAULT '', photo TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE purchase_request_lines (
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id), code TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
 qty INTEGER NOT NULL CHECK(qty > 0), unit TEXT NOT NULL, unit_price REAL NOT NULL DEFAULT 0,
 stock_at_request REAL NOT NULL, min_at_request REAL NOT NULL
);
CREATE TABLE purchase_requests (
 id INTEGER PRIMARY KEY, request_no TEXT UNIQUE, created_at TEXT NOT NULL,
 created_by INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL DEFAULT 'Создана'
, closed_at TEXT, closed_by INTEGER REFERENCES users(id), archived_at TEXT, archived_by INTEGER REFERENCES users(id));
CREATE TABLE role_definitions (
 code TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, permissions_json TEXT NOT NULL DEFAULT '[]', is_system INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sessions (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE submitted_forms (
 user_id INTEGER NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,request_id)
);
CREATE TABLE users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL,
 worker_id INTEGER UNIQUE REFERENCES workers(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE worker_machines (
 worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
 machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
 PRIMARY KEY(worker_id,machine_id)
);
CREATE TABLE workers (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, personnel_no TEXT UNIQUE, machine_id INTEGER REFERENCES machines(id), active INTEGER NOT NULL DEFAULT 1
);
