"""
db.py  –  Database layer for DOTM License Status Checker
SQLite3 + indexed search · Drop-in replaceable with PostgreSQL

Schema:
    licenses (
        id           INTEGER PRIMARY KEY,
        license_no   TEXT    UNIQUE NOT NULL,   ← indexed
        name         TEXT    NOT NULL,
        category     TEXT,
        office       TEXT,
        print_date   TEXT,
        district     TEXT,
        last_updated TEXT
    )

    meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import date

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "licenses.db")

# ─── Connection helper ───────────────────────────────────────────────────────

@contextmanager
def get_conn():
    """Yield a thread-safe SQLite connection with WAL mode for concurrency."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # dict-like rows
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads while writing
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA cache_size=-64000") # 64 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Schema ──────────────────────────────────────────────────────────────────

def init_db():
    """Create tables and indexes if they don't exist yet."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                license_no   TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                name         TEXT    NOT NULL,
                category     TEXT    DEFAULT '',
                office       TEXT    DEFAULT '',
                print_date   TEXT    DEFAULT '',
                district     TEXT    DEFAULT '',
                last_updated TEXT    DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_license_no
                ON licenses (license_no COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
    log.debug("Database initialized: %s", DB_PATH)


# ─── Query ───────────────────────────────────────────────────────────────────

def find_license(license_no: str) -> dict | None:
    """
    Look up a license number.  Returns a dict or None.
    Case-insensitive; strips leading/trailing whitespace.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_no = ? COLLATE NOCASE",
            (license_no.strip(),)
        ).fetchone()
    return dict(row) if row else None


# ─── Bulk upsert ─────────────────────────────────────────────────────────────

def upsert_licenses(records: list[dict]) -> int:
    """
    Insert or update license records.
    `records` is a list of dicts with keys:
        license_no, name, category, office, print_date, district, last_updated

    Returns the number of rows affected.
    """
    if not records:
        return 0

    sql = """
        INSERT INTO licenses
            (license_no, name, category, office, print_date, district, last_updated)
        VALUES
            (:license_no, :name, :category, :office, :print_date, :district, :last_updated)
        ON CONFLICT(license_no) DO UPDATE SET
            name         = excluded.name,
            category     = excluded.category,
            office       = excluded.office,
            print_date   = excluded.print_date,
            district     = excluded.district,
            last_updated = excluded.last_updated
    """

    today = date.today().strftime("%Y-%m-%d")
    for r in records:
        r.setdefault("last_updated", today)
        r.setdefault("district",     "")      # ← safe default, PDF has no district
        r.setdefault("category",     "")
        r.setdefault("office",       "")
        r.setdefault("print_date",   "")
        r.setdefault("name",         "UNKNOWN")

    with get_conn() as conn:
        conn.executemany(sql, records)

    log.info("Upserted %d license records", len(records))
    return len(records)


# ─── Stats ───────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return basic statistics shown on the homepage."""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        meta  = conn.execute("SELECT value FROM meta WHERE key='last_updated'").fetchone()

    return {
        "total_records": total,
        "last_updated":  meta[0] if meta else "—",
    }


def set_meta(key: str, value: str):
    """Persist a key/value pair in the meta table."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )


# ─── Demo / Sample Data ──────────────────────────────────────────────────────

def load_sample_data():
    """
    Populate the database with realistic sample records for demo/testing.
    Remove or replace this with real parsed data in production.
    """
    today = date.today().strftime("%Y-%m-%d")

    samples = [
        {
            "license_no":   "07-01-00012345",
            "name":         "RAM BAHADUR THAPA",
            "category":     "B",
            "office":       "Bagmati Yatayat Sewi Karyalaya",
            "print_date":   "2081-05-15",
            "district":     "Kathmandu",
            "last_updated": today,
        },
        {
            "license_no":   "07-02-00067890",
            "name":         "SITA DEVI SHARMA",
            "category":     "A, B",
            "office":       "Janakpur Yatayat Sewi Karyalaya",
            "print_date":   "2081-04-28",
            "district":     "Dhanusha",
            "last_updated": today,
        },
        {
            "license_no":   "03-01-00099001",
            "name":         "BISHNU PRASAD POUDEL",
            "category":     "K",
            "office":       "Gandaki Yatayat Sewi Karyalaya",
            "print_date":   "2081-03-10",
            "district":     "Kaski",
            "last_updated": today,
        },
        {
            "license_no":   "05-01-00054321",
            "name":         "MINA KUMARI ADHIKARI",
            "category":     "B",
            "office":       "Lumbini Yatayat Sewi Karyalaya",
            "print_date":   "2081-06-01",
            "district":     "Rupandehi",
            "last_updated": today,
        },
        {
            "license_no":   "04-01-00011111",
            "name":         "HARI PRASAD KOIRALA",
            "category":     "A, B, C",
            "office":       "Gandaki Yatayat Sewi Karyalaya",
            "print_date":   "2080-12-22",
            "district":     "Syangja",
            "last_updated": today,
        },
        {
            "license_no":   "01-01-00078900",
            "name":         "LAKSHMI RANA MAGAR",
            "category":     "B",
            "office":       "Koshi Yatayat Sewi Karyalaya",
            "print_date":   "2081-02-14",
            "district":     "Morang",
            "last_updated": today,
        },
        {
            "license_no":   "07-01-00099999",
            "name":         "SURESH KUMAR SHRESTHA",
            "category":     "B, C",
            "office":       "Bagmati Yatayat Sewi Karyalaya",
            "print_date":   "2081-05-30",
            "district":     "Lalitpur",
            "last_updated": today,
        },
    ]

    upsert_licenses(samples)
    set_meta("last_updated", today)
    log.info("Sample data loaded: %d records", len(samples))