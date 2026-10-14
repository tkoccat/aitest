"""SQLite katmanı (demo). Tek süreç (API) veritabanına yazar; ajan API üzerinden konuşur."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    check_in TEXT NOT NULL,          -- YYYY-MM-DD
    check_out TEXT NOT NULL,         -- YYYY-MM-DD
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    price_usd REAL NOT NULL,
    inclusions TEXT NOT NULL DEFAULT '',   -- satır satır, "\\n" ile ayrılmış
    total_rooms INTEGER NOT NULL,
    max_discount_pct REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    rooms INTEGER NOT NULL,
    guest_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'held',   -- held | confirmed | released
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,              -- LiveKit oda adı
    started_at TEXT NOT NULL,
    ended_at TEXT,
    language TEXT NOT NULL DEFAULT '',
    guest_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'info_only',
    summary TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    callback_time TEXT NOT NULL DEFAULT '',
    event_id INTEGER,
    package_id INTEGER,
    hold_id INTEGER,
    transcript TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("DB_PATH", "data/zeynep.db")
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    # autocommit; işlemleri kendimiz yönetiriz. FastAPI bağımlılığı ile uç noktası farklı iş parçacıklarında
    # çalışabildiği için check_same_thread kapalı (bir bağlantıyı aynı anda tek istek kullanır).
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
