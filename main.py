"""
main.py – DOTM Nepal License Status Checker
─────────────────────────────────────────────────────────────────
All app logic lives here:
  • Flask routes (public + admin)
  • SQLite database layer
  • PDF parser
  • PDF source manager (downloads via admin-supplied URLs)
  • PDF upload endpoint (admin can POST a PDF file directly)
  • Keepalive (prevents Render free tier from sleeping)

Records originate from PDF URLs OR direct uploads added by the admin.
PDFs are processed and deleted immediately after records are stored.
"""

import os
import io
import re
import csv
import json
import time
import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

from flask import (
    Flask, request, jsonify,
    render_template, redirect,
    session, Response, abort
)
from flask_cors import CORS


# ══════════════════════════════════════════════════════════════════
#   LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#   CONFIG
# ══════════════════════════════════════════════════════════════════
BASE_DIR          = Path(__file__).parent
DB_PATH           = BASE_DIR / "licenses.db"
PDF_SOURCES_FILE  = BASE_DIR / "pdf_sources.json"
TEMP_DIR          = BASE_DIR / ".pdf_tmp"
UPLOADS_DIR       = BASE_DIR / "uploaded_pdfs"

# Max upload size: 100 MB (adjust as needed)
MAX_UPLOAD_BYTES  = 100 * 1024 * 1024

ADMIN_USERNAME    = os.environ.get("ADMIN_USERNAME", "shushantgiri@admin.com")
ADMIN_PASSWORD    = os.environ.get("ADMIN_PASSWORD", "License@123!@#")
SECRET_KEY        = os.environ.get("SECRET_KEY",     "dotm-secret-change-me-2081")

RECAPTCHA_SITE_KEY   = os.environ.get("RECAPTCHA_SITE_KEY",   "6LcLl9UsAAAAAAD1jK31dpGJSW8a5_9cyKAwiLOy").strip()
RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY", "6LcLl9UsAAAAAJpf_tQzGmeJRo8loEl10ScgCrFw").strip()
try:
    RECAPTCHA_MIN_SCORE = float(os.environ.get("RECAPTCHA_MIN_SCORE", "0.5"))
except ValueError:
    RECAPTCHA_MIN_SCORE = 0.5


# ══════════════════════════════════════════════════════════════════
#   KEEPALIVE
# ══════════════════════════════════════════════════════════════════
def _keepalive_loop():
    time.sleep(90)
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        log.info("Keepalive: RENDER_EXTERNAL_URL not set — skipping")
        return
    ping_url = url + "/healthz"
    log.info("Keepalive started → %s (every 10 min)", ping_url)
    while True:
        try:
            with urlopen(ping_url, timeout=10) as r:
                log.debug("Keepalive OK (%d)", r.status)
        except Exception as e:
            log.debug("Keepalive ping failed: %s", e)
        time.sleep(600)


def start_keepalive():
    if not os.environ.get("RENDER"):
        return
    t = threading.Thread(target=_keepalive_loop, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════
#   DATABASE LAYER
# ══════════════════════════════════════════════════════════════════
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
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

            CREATE INDEX IF NOT EXISTS idx_office
                ON licenses (office);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS search_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                query      TEXT    NOT NULL,
                found      INTEGER NOT NULL DEFAULT 0,
                license_no TEXT    DEFAULT '',
                name       TEXT    DEFAULT '',
                created_at TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_search_log_created
                ON search_log (created_at DESC);

            CREATE TABLE IF NOT EXISTS office_order (
                office      TEXT PRIMARY KEY,
                order_index INTEGER DEFAULT 0
            );
        """)


def find_license(license_no: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_no = ? COLLATE NOCASE",
            (license_no.strip(),)
        ).fetchone()
    return dict(row) if row else None


def upsert_licenses(records: list) -> int:
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
        r.setdefault("district",     "")
        r.setdefault("category",     "")
        r.setdefault("office",       "")
        r.setdefault("print_date",   "")
        r.setdefault("name",         "UNKNOWN")

    with get_conn() as conn:
        conn.executemany(sql, records)

    log.info("Upserted %d license records", len(records))
    return len(records)


def get_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        meta  = conn.execute(
            "SELECT value FROM meta WHERE key='last_updated'"
        ).fetchone()
    return {
        "total_records": total,
        "last_updated":  meta[0] if meta else "—",
    }


def get_office_breakdown() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT office,
                   COALESCE(NULLIF(district, ''), '') AS district,
                   COUNT(*) AS count
            FROM licenses
            WHERE office IS NOT NULL AND office != ''
            GROUP BY office
        """).fetchall()
    rows = [dict(r) for r in rows]
    order_map = get_office_order_map()
    UNRANKED  = 10**6
    rows.sort(key=lambda r: (
        order_map.get(r["office"], UNRANKED),
        -r["count"],
        r["office"].lower(),
    ))
    return rows


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )


def get_office_order_map() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT office, order_index FROM office_order"
        ).fetchall()
    return {r["office"]: r["order_index"] for r in rows}


# ══════════════════════════════════════════════════════════════════
#   PDF PARSER
#
# Two-strategy parser:
#   1. Table extraction via pdfplumber, using detected column headers
#      (sn, license holder name, license number, category, office,
#       license printed date — and common synonyms).
#   2. Line-based regex fallback for PDFs without detectable tables.
# ══════════════════════════════════════════════════════════════════

# Permissive license-number pattern used by the line-based fallback.
LICENSE_RE = re.compile(r'\b(\d{1,3}-\d{1,4}-\d{4,15})\b')

# Used to validate the license_no column from a parsed table row
# (must contain at least one digit).
LICENSE_CELL_RE = re.compile(r'\d')

DATE_RE = re.compile(
    r'\b(\d{4}[-/](?:\d{1,2}|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/]\d{1,2})\b',
    re.IGNORECASE
)

CAT_RE = re.compile(r'\b([A-Z](?:\s*[,/]\s*[A-Z])*)\b', re.IGNORECASE)

MONTH_MAP = {
    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
}

# Column header aliases — headers are normalised (lowercased, non-letters
# stripped) and matched by substring against these. ORDER MATTERS: keys
# whose aliases overlap with another key (e.g. "licenseholdername" contains
# "license") must come BEFORE the broader key, so the more specific match
# wins. Iterated as a dict in Python 3.7+ preserves this order.
HEADER_ALIASES = {
    "name":     ["licenseholdername", "holdername", "fullname",
                 "drivername", "applicantname", "name"],
    "date":     ["licenseprinteddate", "printeddate", "printdate",
                 "issueddate", "issuedate", "date"],
    "license":  ["licensenumber", "licenseno", "licno",
                 "licencenumber", "licenceno", "license", "licence"],
    "category": ["vehiclecategory", "category", "class", "cat"],
    "office":   ["issuingoffice", "transportoffice", "office"],
    "sn":       ["serialnumber", "serialno", "slno", "srno", "sno", "sn"],
}


def _normalize_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    for mon, num in MONTH_MAP.items():
        upper = upper.replace(f'-{mon}-', f'-{num}-') \
                     .replace(f'/{mon}/', f'/{num}/')
    return upper.replace('/', '-')


def _normalize_header(s: str) -> str:
    return re.sub(r'[^a-z]', '', (s or '').lower())


def _classify_header_cell(text: str):
    n = _normalize_header(text)
    if not n:
        return None
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in n:
                return key
    return None


def _build_cols_map(header_row) -> dict:
    """Map column-key → column-index for a row that looks like a header."""
    cols = {}
    for i, cell in enumerate(header_row or []):
        key = _classify_header_cell(cell)
        if key and key not in cols:
            cols[key] = i
    return cols


def _find_header_row(table, max_check: int = 4):
    """Scan first few rows for a header. Returns (cols_map, data_start_index)
    or (None, 0) if no header detected. Requires at minimum the
    'license' and 'name' columns to be present."""
    for r_idx in range(min(max_check, len(table))):
        cols = _build_cols_map(table[r_idx])
        if "license" in cols and "name" in cols:
            return cols, r_idx + 1
    return None, 0


def _parse_table_row(row, cols: dict):
    """Convert one table row → license record dict, or None if invalid."""
    def get(key: str) -> str:
        i = cols.get(key)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    license_no = re.sub(r'\s+', '', get("license"))
    if not license_no or not LICENSE_CELL_RE.search(license_no):
        return None

    name = re.sub(r'\s+', ' ', get("name")).upper().strip()
    if len(name) < 2:
        return None

    return {
        "license_no": license_no,
        "name":       name,
        "category":   re.sub(r'\s+', '', get("category")).upper(),
        "office":     re.sub(r'\s+', ' ', get("office")).strip(),
        "print_date": _normalize_date(get("date")),
    }


def _parse_pdf_via_tables(pdf_path, on_progress=None):
    """Primary strategy: use pdfplumber to extract tables and parse via
    detected column headers. Returns a list of record dicts (may be empty)."""
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — skipping table extraction")
        return []

    records = []
    cols_map: dict = {}

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                try:
                    tables = page.extract_tables() or []
                except Exception as e:
                    log.debug("extract_tables failed on page %d: %s", i, e)
                    tables = []

                for tbl in tables:
                    if not tbl:
                        continue

                    # Try to detect a header in the first few rows of this table.
                    detected, data_start = _find_header_row(tbl)
                    if detected:
                        cols_map = detected
                        data_rows = tbl[data_start:]
                    elif cols_map:
                        # Reuse header from earlier page/table.
                        data_rows = tbl
                    else:
                        # No header yet — skip and try the next table.
                        continue

                    for row in data_rows:
                        if not any((c or "").strip() for c in row):
                            continue
                        rec = _parse_table_row(row, cols_map)
                        if rec:
                            records.append(rec)

                if on_progress and (i % 5 == 0 or i == total - 1):
                    on_progress(phase="parsing", current_page=i + 1,
                                total_pages=total,
                                imported_so_far=len(records))
    except Exception as e:
        log.warning("pdfplumber parse failed: %s", e)
        return []

    return records


def _parse_line(line: str):
    """Fallback line parser — used when no table headers can be detected."""
    m = LICENSE_RE.search(line)
    if not m:
        return None

    license_no = m.group(1)
    before     = line[:m.start()].strip()
    after      = line[m.end():].strip()

    name = re.sub(r'^\d+[\.\)\-\s]+', '', before).strip()
    name = re.sub(r'\s+', ' ', name).upper()
    if len(name) < 2:
        return None

    category = ""
    cat_m    = CAT_RE.match(after)
    if cat_m:
        category = cat_m.group(0).upper().replace(' ', '')
        after    = after[cat_m.end():].strip()

    print_date = ""
    date_m     = DATE_RE.search(after)
    if date_m:
        print_date = _normalize_date(date_m.group(1))
        after      = after[:date_m.start()].strip()

    office = re.sub(r'\s+', ' ', after).strip()
    office = re.sub(r'^[\d\.\)\-]+', '', office).strip()

    return {
        "license_no": license_no,
        "name":       name,
        "category":   category,
        "office":     office,
        "print_date": print_date,
    }


def _parse_page_text(text: str) -> list:
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = _parse_line(line)
        if r and len(r["license_no"]) >= 7:
            records.append(r)
    return records


def _parse_pdf_via_text(pdf_path, on_progress=None):
    """Fallback strategy: page-by-page text + line regex."""
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf_path))
    total  = len(reader.pages)
    records = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
            records.extend(_parse_page_text(text))
        except Exception as e:
            log.warning("Page %d failed: %s", i, e)
        if on_progress and (i % 5 == 0 or i == total - 1):
            on_progress(phase="parsing", current_page=i + 1,
                        total_pages=total, imported_so_far=len(records))
    return records


def parse_and_store(pdf_path, district: str = "", office_override: str = "",
                    batch_size: int = 500, on_progress=None) -> int:
    """Parse a PDF and upsert records in batches.

    Tries header-driven table extraction first (works regardless of
    layout, as long as the columns are labelled). Falls back to the
    line-based regex parser for PDFs without recognisable tables.
    """
    init_db()
    pdf = Path(pdf_path)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    log.info("Parsing PDF: %s", pdf)
    t_start = time.time()
    today   = date.today().strftime("%Y-%m-%d")

    raw = _parse_pdf_via_tables(pdf, on_progress=on_progress)
    strategy = "tables"
    if not raw:
        log.info("Table parse yielded 0 records — falling back to text/regex")
        raw = _parse_pdf_via_text(pdf, on_progress=on_progress)
        strategy = "text"

    log.info("Parser strategy: %s · raw rows: %d", strategy, len(raw))

    # Dedupe by license_no, apply overrides, set defaults.
    deduped: dict = {}
    for r in raw:
        key = (r.get("license_no") or "").upper()
        if not key:
            continue
        r["last_updated"] = today
        if office_override:
            r["office"] = office_override
        if district:
            r["district"] = district
        else:
            r.setdefault("district", "")
        deduped[key] = r

    # Upsert in batches.
    items = list(deduped.values())
    total_imported = 0
    for i in range(0, len(items), batch_size):
        total_imported += upsert_licenses(items[i:i + batch_size])

    if total_imported == 0:
        log.warning("No records extracted — check PDF format!")
        return 0

    set_meta("last_updated", today)
    log.info("Done in %.1fs — %d records imported (strategy: %s)",
             time.time() - t_start, total_imported, strategy)
    return total_imported


# ══════════════════════════════════════════════════════════════════
#   PDF SOURCES
# ══════════════════════════════════════════════════════════════════
def load_pdf_sources() -> dict:
    if PDF_SOURCES_FILE.exists():
        try:
            raw        = json.loads(PDF_SOURCES_FILE.read_text(encoding="utf-8"))
            normalised = {}
            for idx, (name, val) in enumerate(raw.items()):
                if isinstance(val, str):
                    entry = {"url": val, "district": "", "order": idx, "source_type": "url"}
                else:
                    entry = dict(val)
                    entry.setdefault("district",    "")
                    entry.setdefault("order",       idx)
                    entry.setdefault("source_type", "url")
                normalised[name] = entry
            return dict(sorted(
                normalised.items(),
                key=lambda kv: (int(kv[1].get("order", 0)), kv[0].lower())
            ))
        except Exception:
            pass
    return {}


def save_pdf_sources(sources: dict):
    PDF_SOURCES_FILE.write_text(
        json.dumps(sources, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def sync_office_order_from_sources(sources: dict):
    with get_conn() as conn:
        conn.execute("DELETE FROM office_order")
        for name, entry in sources.items():
            conn.execute(
                "INSERT INTO office_order (office, order_index) VALUES (?, ?)",
                (name, int(entry.get("order", 0))),
            )


def _safe_office_slug(office: str) -> str:
    return re.sub(r'[^\w]', '_', (office or "").lower())


def _temp_pdf_path(office: str) -> Path:
    TEMP_DIR.mkdir(exist_ok=True)
    return TEMP_DIR / f"{_safe_office_slug(office)}.pdf"


def _uploaded_pdf_path(office: str) -> Path:
    """Permanent storage path for a PDF that the admin uploaded directly.
    Kept on disk so it can be re-parsed during a future sync."""
    UPLOADS_DIR.mkdir(exist_ok=True)
    return UPLOADS_DIR / f"{_safe_office_slug(office)}.pdf"


def _delete_temp_pdf(path: Path):
    try:
        if path.exists():
            path.unlink()
            log.info("Deleted temp PDF: %s", path.name)
    except OSError as e:
        log.warning("Could not delete temp PDF %s: %s", path.name, e)


def _delete_uploaded_pdf(office: str):
    path = _uploaded_pdf_path(office)
    try:
        if path.exists():
            path.unlink()
            log.info("Deleted uploaded PDF: %s", path.name)
    except OSError as e:
        log.warning("Could not delete uploaded PDF %s: %s", path.name, e)


def _download_pdf_to_file(url: str, dest: Path, headers: dict,
                          chunk_size: int = 65536):
    """Stream a PDF to disk in chunks. Returns (ok, bytes_written, error_msg)."""
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=60) as resp:
            head = resp.read(8)
            if not head.startswith(b"%PDF"):
                return False, len(head), "Response was not a valid PDF"
            size = 0
            with open(part, "wb") as f:
                f.write(head)
                size += len(head)
                while True:
                    buf = resp.read(chunk_size)
                    if not buf:
                        break
                    f.write(buf)
                    size += len(buf)
        if size <= 512:
            try:
                part.unlink()
            except OSError:
                pass
            return False, size, "PDF too small (likely empty)"
        part.replace(dest)
        return True, size, ""
    except (HTTPError, URLError, OSError) as e:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return False, 0, str(e)


# ══════════════════════════════════════════════════════════════════
#   reCAPTCHA v3
# ══════════════════════════════════════════════════════════════════
def verify_recaptcha(token: str, remote_ip: str = ""):
    if not RECAPTCHA_SECRET_KEY:
        return True, 1.0, "disabled"
    if not token:
        return False, 0.0, "missing-token"

    payload = urlencode({
        "secret":   RECAPTCHA_SECRET_KEY,
        "response": token,
        "remoteip": remote_ip or "",
    }).encode("utf-8")

    try:
        req = Request(
            "https://www.google.com/recaptcha/api/siteverify",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("reCAPTCHA verify HTTP error: %s", e)
        return False, 0.0, "verify-http-error"

    score = float(data.get("score") or 0.0)
    if not bool(data.get("success")):
        codes = ",".join(data.get("error-codes") or []) or "verify-failed"
        return False, score, codes
    if score < RECAPTCHA_MIN_SCORE:
        return False, score, "low-score"
    return True, score, ""


# ══════════════════════════════════════════════════════════════════
#   AUTH
# ══════════════════════════════════════════════════════════════════
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect("/dotm-admin/login")
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════
#   FLASK APP
# ══════════════════════════════════════════════════════════════════
app = Flask(
    __name__,
    template_folder=BASE_DIR / "templates",
    static_folder=BASE_DIR   / "static",
    static_url_path="/static",
)
app.secret_key              = SECRET_KEY
app.json.sort_keys          = False
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
CORS(app)


@app.before_request
def _ensure_db():
    init_db()


# ══════════════════════════════════════════════════════════════════
#   PWA
# ══════════════════════════════════════════════════════════════════
@app.route("/sw.js")
def service_worker():
    response = app.send_static_file("sw.js")
    response.headers["Content-Type"]        = "application/javascript"
    response.headers["Cache-Control"]       = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/manifest.json")
def manifest():
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(BASE_DIR, "static"),
        "manifest.json",
        mimetype="application/json"
    )


# ══════════════════════════════════════════════════════════════════
#   PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html", recaptcha_site_key=RECAPTCHA_SITE_KEY)


@app.route("/api/check")
def api_check():
    raw = request.args.get("license", "").strip()
    if not raw:
        return jsonify({"error": "License number required"}), 400

    token = (
        request.headers.get("X-Captcha-Token", "").strip()
        or request.args.get("captcha", "").strip()
    )
    remote_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr or ""
    )
    ok, score, err = verify_recaptcha(token, remote_ip=remote_ip)
    if not ok:
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO search_log "
                    "(query, found, license_no, name, created_at) VALUES (?,?,?,?,?)",
                    (raw[:128], 0, "[blocked]",
                     f"captcha:{err}:{score:.2f}"[:128],
                     datetime.utcnow().isoformat() + "Z"),
                )
        except Exception:
            pass
        return jsonify({"found": False, "error": "Verification failed.", "code": "captcha"}), 403

    license_no = raw.upper().replace(" ", "")
    record     = find_license(license_no)

    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO search_log "
                "(query, found, license_no, name, created_at) VALUES (?,?,?,?,?)",
                (raw[:128], 1 if record else 0,
                 (record.get("license_no") if record else license_no)[:64],
                 (record.get("name")       if record else "")[:128],
                 datetime.utcnow().isoformat() + "Z"),
            )
    except Exception as e:
        log.warning("search_log insert failed: %s", e)

    if record:
        return jsonify({
            "found":        True,
            "license_no":   record.get("license_no"),
            "name":         record.get("name"),
            "category":     record.get("category"),
            "office":       record.get("office"),
            "print_date":   record.get("print_date"),
            "district":     record.get("district"),
            "last_updated": record.get("last_updated"),
        })
    return jsonify({"found": False})


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/offices")
def api_offices():
    rows  = get_office_breakdown()
    total = sum(r["count"] for r in rows)
    return jsonify({"offices": rows, "total": total})

# Add Bulk Sync API
@app.route("/api/licenses")
def api_all_licenses():
    """
    Returns all licenses (or paginated) for offline sync.
    WARNING: Use limit in production if DB is huge.
    """
    try:
        limit = int(request.args.get("limit", 5000))
        offset = int(request.args.get("offset", 0))

        with get_conn() as conn:
            rows = conn.execute("""
                SELECT license_no, name, category, office, print_date, district, last_updated
                FROM licenses
                ORDER BY license_no
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        response = jsonify({
            "data": [dict(r) for r in rows],
            "limit": limit,
            "offset": offset,
            "count": len(rows)
        })
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# This avoids downloading everything every time.
@app.route("/api/last-updated")
def api_last_updated():
    stats = get_stats()
    return jsonify({
        "last_updated": stats.get("last_updated")
    })
# Add Search API Without Captcha for internal use (e.g. admin dashboard) — rate-limited    
@app.route("/api/search-lite")
def api_search_lite():
    license_no = request.args.get("license", "").strip().upper()

    if not license_no:
        return jsonify({"found": False})

    record = find_license(license_no)

    if record:
        return jsonify({"found": True, "data": record})

    return jsonify({"found": False})

@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"})


@app.route("/admin", defaults={"subpath": ""})
@app.route("/admin/<path:subpath>")
def admin_decoy(subpath):
    abort(404)


# ══════════════════════════════════════════════════════════════════
#   ADMIN AUTH
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/login", methods=["GET"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect("/dotm-admin")
    return render_template("admin_login.html")


@app.route("/dotm-admin/login", methods=["POST"])
def admin_login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        session.permanent          = True
        log.info("Admin login: %s", username)
        return redirect("/dotm-admin")
    log.warning("Failed admin login: %s", username)
    return redirect("/dotm-admin/login?error=1")


@app.route("/dotm-admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect("/dotm-admin/login")


# ══════════════════════════════════════════════════════════════════
#   ADMIN DASHBOARD
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html")


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — PDF SOURCES (URL-based)
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/urls", methods=["GET"])
@admin_required
def admin_get_urls():
    return jsonify({"urls": load_pdf_sources()})


@app.route("/dotm-admin/api/urls", methods=["POST"])
@admin_required
def admin_add_url():
    data     = request.get_json() or {}
    name     = data.get("name",     "").strip()
    url      = data.get("url",      "").strip()
    district = data.get("district", "").strip()
    raw_order = data.get("order")

    if not name or not url:
        return jsonify({"ok": False, "error": "Name and URL required"}), 400
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    sources = load_pdf_sources()

    desired_slot = None
    if raw_order not in (None, ""):
        try:
            n = int(raw_order)
            if n >= 1:
                desired_slot = min(n - 1, len(sources))
        except (TypeError, ValueError):
            pass

    if desired_slot is None:
        new_order = max(
            (int(s.get("order", 0)) for s in sources.values()), default=-1) + 1
    else:
        for s in sources.values():
            cur = int(s.get("order", 0))
            if cur >= desired_slot:
                s["order"] = cur + 1
        new_order = desired_slot

    sources[name] = {
        "url":         url,
        "district":    district,
        "order":       new_order,
        "source_type": "url",
    }
    save_pdf_sources(sources)
    sync_office_order_from_sources(sources)
    log.info("Added source: %s → %s (district: %s, order: %d)",
             name, url[:60], district, new_order)
    return jsonify({"ok": True, "order": new_order + 1})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — PDF UPLOAD (file-based source)
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/upload-pdf", methods=["POST"])
@admin_required
def admin_upload_pdf():
    """
    Combined upload + register-source endpoint.

    Accepts a multipart/form-data POST with:
      - file     : the PDF file (single)
      - name     : office name (required)
      - district : district name (optional)
      - order    : 1-based desired position (optional)

    Saves the PDF to UPLOADS_DIR (kept on disk so future syncs can
    re-parse it), parses it into the DB, then registers the office in
    pdf_sources.json with source_type='upload'.
    """
    name     = request.form.get("name",     "").strip()
    district = request.form.get("district", "").strip()
    raw_order = request.form.get("order")

    if not name:
        return jsonify({"ok": False, "error": "Office name is required"}), 400

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    # Validate it's a PDF by magic bytes.
    header = f.read(8)
    if not header.startswith(b"%PDF"):
        return jsonify({"ok": False, "error": "Uploaded file is not a valid PDF"}), 400
    f.seek(0)

    sources = load_pdf_sources()
    if name in sources:
        return jsonify({
            "ok":    False,
            "error": f'A source named "{name}" already exists. '
                     f'Remove it first if you want to re-upload.',
        }), 409

    dest = _uploaded_pdf_path(name)

    try:
        f.save(str(dest))
        file_size = dest.stat().st_size
        log.info("Upload received: %s — %.1f KB", name, file_size / 1024)

        if file_size < 512:
            _delete_uploaded_pdf(name)
            return jsonify({"ok": False, "error": "PDF too small (likely empty)"}), 400

        # Parse and store records into the DB.
        n = parse_and_store(dest, district=district, office_override=name)

        if n == 0:
            _delete_uploaded_pdf(name)
            return jsonify({
                "ok":      False,
                "error":   ("No license records could be extracted from this PDF. "
                            "Make sure it includes columns labelled with the license "
                            "number and license holder name (Category / Office / "
                            "Printed Date are also recommended)."),
                "records": 0,
            }), 422

        # Decide ordering slot (same logic as URL add).
        desired_slot = None
        if raw_order not in (None, ""):
            try:
                n_ord = int(raw_order)
                if n_ord >= 1:
                    desired_slot = min(n_ord - 1, len(sources))
            except (TypeError, ValueError):
                pass

        if desired_slot is None:
            new_order = max(
                (int(s.get("order", 0)) for s in sources.values()), default=-1) + 1
        else:
            for s in sources.values():
                cur = int(s.get("order", 0))
                if cur >= desired_slot:
                    s["order"] = cur + 1
            new_order = desired_slot

        sources[name] = {
            "url":         "",
            "district":    district,
            "order":       new_order,
            "source_type": "upload",
        }
        save_pdf_sources(sources)
        sync_office_order_from_sources(sources)

        log.info("Upload import done: %s — %d records", name, n)
        return jsonify({
            "ok":       True,
            "records":  n,
            "office":   name,
            "district": district,
            "order":    new_order + 1,
        })

    except Exception as e:
        log.exception("Upload parse failed [%s]", name)
        # Clean up the half-imported file so the office isn't half-registered.
        _delete_uploaded_pdf(name)
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — REORDER / RENAME / DELETE sources
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/urls/reorder", methods=["POST"])
@admin_required
def admin_reorder_urls():
    data  = request.get_json() or {}
    names = data.get("names")
    if not isinstance(names, list):
        return jsonify({"ok": False, "error": "names must be a list"}), 400

    sources    = load_pdf_sources()
    seen       = set()
    next_order = 0
    for name in names:
        if name in sources and name not in seen:
            sources[name]["order"] = next_order
            seen.add(name)
            next_order += 1
    for name in sources:
        if name not in seen:
            sources[name]["order"] = next_order
            next_order += 1
    save_pdf_sources(sources)
    sync_office_order_from_sources(sources)
    return jsonify({"ok": True, "order": list(load_pdf_sources().keys())})


@app.route("/dotm-admin/api/offices/reorder", methods=["POST"])
@admin_required
def admin_reorder_offices():
    data    = request.get_json() or {}
    offices = data.get("offices")
    if not isinstance(offices, list):
        return jsonify({"ok": False, "error": "offices must be a list"}), 400

    with get_conn() as conn:
        for idx, office in enumerate(offices):
            conn.execute(
                "INSERT OR REPLACE INTO office_order (office, order_index) VALUES (?, ?)",
                (office, idx)
            )
    return jsonify({"ok": True})


@app.route("/dotm-admin/api/urls/<name>", methods=["PUT"])
@admin_required
def admin_rename_url(name=None):
    data     = request.get_json() or {}
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "New name required"}), 400

    sources = load_pdf_sources()
    if name not in sources:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if new_name == name:
        return jsonify({"ok": True, "records_updated": 0, "unchanged": True})
    if new_name in sources:
        return jsonify({"ok": False, "error": "An office with that name already exists"}), 409

    renamed = {}
    for k, v in sources.items():
        renamed[new_name if k == name else k] = v

    updated_records = 0
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE licenses SET office = ? WHERE office = ?", (new_name, name))
            updated_records = cur.rowcount
            conn.execute("DELETE FROM office_order WHERE office = ?", (new_name,))
            conn.execute(
                "UPDATE office_order SET office = ? WHERE office = ?", (new_name, name))
    except Exception as e:
        log.error("Failed to rename office '%s' → '%s': %s", name, new_name, e)
        return jsonify({"ok": False, "error": str(e)}), 500

    save_pdf_sources(renamed)

    old_pdf = _temp_pdf_path(name)
    new_pdf = _temp_pdf_path(new_name)
    try:
        if old_pdf.exists():
            old_pdf.replace(new_pdf)
    except OSError as e:
        log.warning("Could not rename cached PDF %s → %s: %s",
                    old_pdf.name, new_pdf.name, e)

    old_upload = _uploaded_pdf_path(name)
    new_upload = _uploaded_pdf_path(new_name)
    try:
        if old_upload.exists():
            old_upload.replace(new_upload)
    except OSError as e:
        log.warning("Could not rename uploaded PDF %s → %s: %s",
                    old_upload.name, new_upload.name, e)

    log.info("Renamed source: '%s' → '%s' (%d records updated)",
             name, new_name, updated_records)
    return jsonify({"ok": True, "records_updated": updated_records})


@app.route("/dotm-admin/api/urls/<name>", methods=["DELETE"])
@admin_required
def admin_del_url(name=None):
    sources = load_pdf_sources()
    if name not in sources:
        return jsonify({"ok": False, "error": "Not found"}), 404
    _delete_temp_pdf(_temp_pdf_path(name))
    _delete_uploaded_pdf(name)
    deleted_records = 0
    try:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM licenses WHERE office = ?", (name,))
            deleted_records = cur.rowcount
    except Exception as e:
        log.error("Failed to delete DB records for %s: %s", name, e)
    del sources[name]
    save_pdf_sources(sources)
    with get_conn() as conn:
        conn.execute("DELETE FROM office_order WHERE office = ?", (name,))
    log.info("Removed source: %s (%d records deleted)", name, deleted_records)
    return jsonify({"ok": True, "records_deleted": deleted_records})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SYNC (background job — URL sources only)
# ══════════════════════════════════════════════════════════════════
SYNC_LOCK  = threading.Lock()
SYNC_STATE: dict = {"status": "idle", "id": None}


def _new_job_state(office_total: int) -> dict:
    return {
        "id":               hashlib.md5(str(time.time()).encode()).hexdigest()[:12],
        "status":           "running",
        "started_at":       datetime.utcnow().isoformat() + "Z",
        "finished_at":      None,
        "current_office":   None,
        "current_phase":    None,
        "current_page":     0,
        "total_pages":      0,
        "office_index":     0,
        "office_total":     office_total,
        "pdfs_downloaded":  0,
        "pdfs_skipped":     0,
        "pdfs_failed":      0,
        "records_imported": 0,
        "results":          [],
        "log":              [],
        "error":            None,
    }


def _job_log(msg: str, level: str = "info"):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    with SYNC_LOCK:
        entries = SYNC_STATE.setdefault("log", [])
        entries.append({"ts": ts, "level": level, "msg": msg})
        if len(entries) > 300:
            del entries[:-300]
    log.info("[sync] %s", msg)


def _job_set(**kwargs):
    with SYNC_LOCK:
        SYNC_STATE.update(kwargs)


def _run_sync_job(sources: dict):
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36",
        "Referer":    "https://dotm.gov.np/",
    }
    TEMP_DIR.mkdir(exist_ok=True)
    try:
        for idx, (office, entry) in enumerate(sources.items(), 1):
            source_type = entry.get("source_type", "url") if isinstance(entry, dict) else "url"
            url         = entry.get("url", "")            if isinstance(entry, dict) else entry
            district    = entry.get("district", "")       if isinstance(entry, dict) else ""

            _job_set(current_office=office, office_index=idx,
                     current_phase="checking",
                     current_page=0, total_pages=0)

            # Uploaded offices: re-parse from the stored PDF on disk.
            if source_type == "upload":
                stored = _uploaded_pdf_path(office)
                if not stored.exists():
                    try:
                        with get_conn() as conn:
                            existing = conn.execute(
                                "SELECT COUNT(*) FROM licenses WHERE office = ?", (office,)
                            ).fetchone()[0]
                    except Exception:
                        existing = 0
                    _job_log(
                        f"[SKIP] {office} — uploaded PDF missing on disk "
                        f"({existing} existing records)", "warn")
                    with SYNC_LOCK:
                        SYNC_STATE["pdfs_skipped"] += 1
                        SYNC_STATE["results"].append({
                            "office":  office, "status": "skipped",
                            "records": existing,
                            "message": f"Uploaded file missing — {existing} records already in DB",
                        })
                    continue

                _job_log(f"[REPARSE] {office} — re-parsing uploaded PDF")
                _job_set(current_phase="parsing")
                try:
                    n = parse_and_store(
                        stored, district=district, office_override=office,
                        on_progress=lambda **kw: _job_set(**kw),
                    )
                    with SYNC_LOCK:
                        SYNC_STATE["pdfs_downloaded"] += 1
                        SYNC_STATE["records_imported"] += n
                        SYNC_STATE["results"].append({
                            "office":  office, "status": "imported",
                            "records": n,
                            "message": f"{n} records re-imported from uploaded PDF",
                        })
                    _job_log(f"[OK] {office} — re-imported {n} records", "ok")
                except Exception as e:
                    log.exception("Re-parse failed [%s]", office)
                    with SYNC_LOCK:
                        SYNC_STATE["pdfs_failed"] += 1
                        SYNC_STATE["results"].append({
                            "office":  office, "status": "parse_failed",
                            "records": 0, "message": str(e),
                        })
                    _job_log(f"[FAIL] {office} — re-parse error: {e}", "err")
                continue

            if not url:
                _job_log(f"[SKIP] {office} — no URL configured", "warn")
                with SYNC_LOCK:
                    SYNC_STATE["pdfs_skipped"] += 1
                    SYNC_STATE["results"].append({
                        "office": office, "status": "skipped",
                        "records": 0, "message": "No URL configured",
                    })
                continue

            tmp_pdf = _temp_pdf_path(office)

            try:
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM licenses WHERE office = ?", (office,)
                    ).fetchone()[0]
            except Exception:
                existing = 0

            if existing > 0:
                _job_log(
                    f"[SKIP] {office} — {existing} records already loaded", "warn")
                with SYNC_LOCK:
                    SYNC_STATE["pdfs_skipped"] += 1
                    SYNC_STATE["results"].append({
                        "office": office, "status": "skipped",
                        "records": existing,
                        "message": f"{existing} records already loaded",
                    })
                continue

            _job_log(f"[DOWNLOAD] {office} — fetching {url[:80]}")
            _job_set(current_phase="downloading")
            ok, nbytes, err = _download_pdf_to_file(url, tmp_pdf, headers)
            if not ok:
                _job_log(f"[FAIL] {office} — download failed: {err}", "err")
                with SYNC_LOCK:
                    SYNC_STATE["pdfs_failed"] += 1
                    SYNC_STATE["results"].append({
                        "office": office, "status": "download_failed",
                        "records": 0, "message": err,
                    })
                continue
            _job_log(f"[OK] {office} — downloaded {nbytes:,} bytes", "ok")

            _job_set(current_phase="parsing")
            try:
                n = parse_and_store(
                    tmp_pdf, district=district, office_override=office,
                    on_progress=lambda **kw: _job_set(**kw),
                )
                with SYNC_LOCK:
                    SYNC_STATE["pdfs_downloaded"] += 1
                    SYNC_STATE["records_imported"] += n
                    SYNC_STATE["results"].append({
                        "office": office, "status": "imported",
                        "records": n, "message": f"{n} records imported",
                    })
                _job_log(f"[OK] {office} — imported {n} records", "ok")
            except Exception as e:
                log.exception("Parse failed [%s]", office)
                with SYNC_LOCK:
                    SYNC_STATE["pdfs_failed"] += 1
                    SYNC_STATE["results"].append({
                        "office": office, "status": "parse_failed",
                        "records": 0, "message": str(e),
                    })
                _job_log(f"[FAIL] {office} — parse error: {e}", "err")
            finally:
                _delete_temp_pdf(tmp_pdf)

        _job_log("Sync complete.", "ok")
        _job_set(status="done", current_phase=None, current_office=None,
                 finished_at=datetime.utcnow().isoformat() + "Z")
    except Exception as e:
        log.exception("Sync job crashed")
        _job_log(f"Sync crashed: {e}", "err")
        _job_set(status="error", error=str(e),
                 finished_at=datetime.utcnow().isoformat() + "Z")


@app.route("/dotm-admin/api/sync", methods=["POST"])
@admin_required
def admin_sync():
    sources = load_pdf_sources()
    if not sources:
        return jsonify({"ok": False, "error": "No PDF sources configured."}), 400

    with SYNC_LOCK:
        if SYNC_STATE.get("status") == "running":
            return jsonify({
                "ok":     False,
                "error":  "A sync is already running.",
                "job_id": SYNC_STATE.get("id"),
            }), 409
        SYNC_STATE.clear()
        SYNC_STATE.update(_new_job_state(len(sources)))
        job_id = SYNC_STATE["id"]

    threading.Thread(
        target=_run_sync_job, args=(sources,), daemon=True
    ).start()

    return jsonify({
        "ok":           True,
        "job_id":       job_id,
        "status":       "started",
        "office_total": len(sources),
    })


@app.route("/dotm-admin/api/sync/status", methods=["GET"])
@admin_required
def admin_sync_status():
    with SYNC_LOCK:
        snap          = dict(SYNC_STATE)
        snap["log"]     = list(snap.get("log") or [])
        snap["results"] = list(snap.get("results") or [])
    return jsonify({"ok": True, "job": snap})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SEARCH
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/search")
@admin_required
def admin_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    pattern = f"%{q}%"
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT license_no, name, category, office, print_date, district
                FROM licenses
                WHERE license_no LIKE ?
                   OR name       LIKE ?
                ORDER BY name
                LIMIT 50
            """, (pattern, pattern)).fetchall()
        return jsonify({"results": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SEARCH LOG
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/search-log")
@admin_required
def admin_search_log():
    try:
        limit  = max(1, min(int(request.args.get("limit",  200) or 200), 1000))
    except ValueError:
        limit  = 200
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        offset = 0
    q = request.args.get("q", "").strip()

    with get_conn() as conn:
        if q:
            pattern = f"%{q}%"
            rows  = conn.execute("""
                SELECT id, query, found, license_no, name, created_at
                FROM search_log
                WHERE query LIKE ? OR license_no LIKE ? OR name LIKE ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
            """, (pattern, pattern, pattern, limit, offset)).fetchall()
            total = conn.execute("""
                SELECT COUNT(*) FROM search_log
                WHERE query LIKE ? OR license_no LIKE ? OR name LIKE ?
            """, (pattern, pattern, pattern)).fetchone()[0]
        else:
            rows  = conn.execute("""
                SELECT id, query, found, license_no, name, created_at
                FROM search_log
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
    return jsonify({"rows": [dict(r) for r in rows], "total": total})


@app.route("/dotm-admin/api/search-log/clear", methods=["POST"])
@admin_required
def admin_clear_search_log():
    try:
        with get_conn() as conn:
            cur     = conn.execute("DELETE FROM search_log")
            deleted = cur.rowcount
        log.info("Search log cleared (%d rows)", deleted)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — BREAKDOWN
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/breakdown")
@admin_required
def admin_breakdown():
    try:
        return jsonify({"rows": get_office_breakdown()})
    except Exception as e:
        return jsonify({"rows": [], "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — EXPORT CSV
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/export")
@admin_required
def admin_export():
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["license_no", "name", "category",
                         "office", "print_date", "district", "last_updated"])
        yield output.getvalue()
        output.truncate(0); output.seek(0)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT license_no,name,category,office,print_date,district,last_updated "
                "FROM licenses ORDER BY license_no"
            ):
                writer.writerow([
                    row["license_no"], row["name"],       row["category"],
                    row["office"],     row["print_date"], row["district"],
                    row["last_updated"]
                ])
                yield output.getvalue()
                output.truncate(0); output.seek(0)
        finally:
            conn.close()

    filename = f"dotm_licenses_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — CLEAR DB
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/clear", methods=["POST"])
@admin_required
def admin_clear():
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM licenses")
            conn.execute("DELETE FROM meta")
            conn.execute("DELETE FROM office_order")
        deleted_files = 0
        if TEMP_DIR.exists():
            for f in TEMP_DIR.glob("*.pdf"):
                try:
                    f.unlink()
                    deleted_files += 1
                except OSError:
                    pass
        log.warning("Admin cleared DB + %d temp PDFs", deleted_files)
        return jsonify({"ok": True, "temp_pdfs_deleted": deleted_files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(413)
def too_large(e):
    return jsonify({
        "ok":    False,
        "error": f"File too large. Maximum allowed size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.",
    }), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error"}), 500


# ══════════════════════════════════════════════════════════════════
#   ENTRY POINT
# ══════════════════════════════════════════════════════════════════
def migrate_office_order():
    sync_office_order_from_sources(load_pdf_sources())


if __name__ == "__main__":
    init_db()
    migrate_office_order()
    start_keepalive()

    port  = int(os.environ.get("PORT",  5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    log.info("Server  →  http://0.0.0.0:%d", port)
    log.info("Admin   →  http://localhost:%d/dotm-admin", port)
    log.info("Login   →  %s / %s", ADMIN_USERNAME, ADMIN_PASSWORD)
    app.run(host="0.0.0.0", port=port, debug=debug)