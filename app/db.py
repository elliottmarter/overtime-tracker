import os
import secrets
import sqlite3

from flask import g

DB_PATH = os.environ.get("DATABASE_PATH", "/data/overtime.db")

# Money is stored in pence (integers) so totals never drift from rounding.
SCHEMA = """
CREATE TABLE IF NOT EXISTS payslips (
    id INTEGER PRIMARY KEY,
    pay_date TEXT NOT NULL,
    covers_month TEXT,
    hours_paid REAL,
    amount_paid INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payslips_date ON payslips(pay_date);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    work_date TEXT NOT NULL,
    hours REAL NOT NULL,
    rate INTEGER NOT NULL,
    ticket TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    payslip_id INTEGER REFERENCES payslips(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(work_date);
CREATE INDEX IF NOT EXISTS idx_entries_payslip ON entries(payslip_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_RATE_PENCE = 2500  # £25.00 an hour


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Create tables and return the persistent session secret."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('secret_key', ?)",
            (secrets.token_hex(32),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('rate', ?)",
            (str(DEFAULT_RATE_PENCE),),
        )
        conn.commit()
        return conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()[0]
    finally:
        conn.close()


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
