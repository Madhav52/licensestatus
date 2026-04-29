"""
main.py – DOTM Nepal License Status Checker (single-file edition)
─────────────────────────────────────────────────────────────────
All app logic lives here:
  • Flask routes (public + admin)
  • SQLite database layer
  • PDF parser
  • PDF source manager (downloads via admin-supplied URLs)
  • Keepalive (prevents Render free tier from sleeping)

Records originate exclusively from PDF URLs added by the admin —
no sample data is ever loaded.

PDFs are downloaded temporarily and deleted immediately after
records are stored in the database — no local PDF files are kept.
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
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "licenses.db"
PDF_SOURCES_FILE = BASE_DIR / "pdf_sources.json"
TEMP_DIR = BASE_DIR / ".pdf_tmp"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "shushantgiri@admin.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "License@123!@#")
SECRET_KEY = os.environ.get("SECRET_KEY",     "dotm-secret-change-me-2081")

# reCAPTCHA v3 — bot mitigation for /api/check.
# When RECAPTCHA_SECRET_KEY is unset the gate is disabled (dev-friendly).
RECAPTCHA_SITE_KEY = os.environ.get(
    "RECAPTCHA_SITE_KEY", "6LfWaNAsAAAAADBb_hWUct9kkawR95qaRxUSbwt6").strip()
RECAPTCHA_SECRET_KEY = os.environ.get(
    "RECAPTCHA_SECRET_KEY", "6LfWaNAsAAAAANP1XXVqSCbk7WPu1mUz3_CPWYAJ").strip()
try:
    RECAPTCHA_MIN_SCORE = float(os.environ.get("RECAPTCHA_MIN_SCORE", "0.5"))
except ValueError:
    RECAPTCHA_MIN_SCORE = 0.5


# ══════════════════════════════════════════════════════════════════
#   KEEPALIVE  (prevents Render free tier from sleeping)
# ══════════════════════════════════════════════════════════════════
def _keepalive_loop():
    """Ping own /healthz every 10 min so Render never spins down."""
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
    """Start the background keepalive thread (only on Render)."""
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
        """)


def find_license(license_no: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_no = ? COLLATE NOCASE",
            (license_no.strip(),)
        ).fetchone()
    return dict(row) if row else None


def upsert_licenses(records: list[dict]) -> int:
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
        meta = conn.execute(
            "SELECT value FROM meta WHERE key='last_updated'"
        ).fetchone()
    return {
        "total_records": total,
        "last_updated":  meta[0] if meta else "—",
    }


def get_office_breakdown() -> list[dict]:
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
    sources = load_pdf_sources()
    order_map = {n: int(e.get("order", 9999))
                 for n, e in sources.items()}
    UNRANKED = 10**6
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


# ══════════════════════════════════════════════════════════════════
#   PDF PARSER
# ══════════════════════════════════════════════════════════════════
LICENSE_RE = re.compile(r'\b(\d{1,2}-\d{2,3}-\d{5,10})\b')

DATE_RE = re.compile(
    r'\b(\d{4}[-/](?:\d{1,2}|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/]\d{1,2})\b',
    re.IGNORECASE
)

CAT_RE = re.compile(r'\b([A-GKa-gk](?:\s*[,/]\s*[A-GKa-gk])*)\b')

MONTH_MAP = {
    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
}


def _normalize_date(raw: str) -> str:
    raw = raw.strip()
    for mon, num in MONTH_MAP.items():
        raw = raw.upper().replace(
            f'-{mon}-', f'-{num}-').replace(f'/{mon}/', f'/{num}/')
    return raw.replace('/', '-')


def _parse_line(line: str):
    today = date.today().strftime("%Y-%m-%d")
    m = LICENSE_RE.search(line)
    if not m:
        return None

    license_no = m.group(1)
    before = line[:m.start()].strip()
    after = line[m.end():].strip()

    name = re.sub(r'^\d+[\.\)\-\s]+', '', before).strip()
    name = re.sub(r'\s+', ' ', name).upper()
    if len(name) < 2:
        return None

    category = ""
    cat_m = CAT_RE.match(after)
    if cat_m:
        category = cat_m.group(0).upper().replace(' ', '')
        after = after[cat_m.end():].strip()

    print_date = ""
    date_m = DATE_RE.search(after)
    if date_m:
        print_date = _normalize_date(date_m.group(1))
        after = after[:date_m.start()].strip()

    office = re.sub(r'\s+', ' ', after).strip()
    office = re.sub(r'^[\d\.\)\-]+', '', office).strip()

    return {
        "license_no": license_no,
        "name":       name,
        "category":   category,
        "office":     office,
        "print_date": print_date,
        "last_updated": today,
    }


def _parse_page_text(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = _parse_line(line)
        if r and len(r["license_no"]) >= 7:
            records.append(r)
    return records


def parse_and_store(pdf_path, district: str = "", office_override: str = "",
                    batch_size: int = 500, on_progress=None) -> int:
    """Parse a PDF page-by-page and upsert in batches.

    Memory stays flat regardless of PDF size: we never accumulate the whole
    record set in RAM. Within a batch, duplicate license_no entries collapse
    to the latest row; duplicates that span batches are resolved by SQLite's
    ON CONFLICT...DO UPDATE.
    """
    init_db()
    pdf = Path(pdf_path)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    log.info("Parsing PDF: %s", pdf)
    t_start = time.time()

    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf))
    total_pages = len(reader.pages)
    log.info("Total pages: %d", total_pages)
    if on_progress:
        on_progress(phase="parsing", current_page=0, total_pages=total_pages)

    batch: dict[str, dict] = {}
    total_imported = 0

    def flush():
        nonlocal total_imported
        if not batch:
            return
        for r in batch.values():
            if office_override:
                r["office"] = office_override
            if district:
                r["district"] = district
        n = upsert_licenses(list(batch.values()))
        total_imported += n
        batch.clear()

    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
            for r in _parse_page_text(text):
                key = r.get("license_no", "").upper()
                if not key:
                    continue
                batch[key] = r
        except Exception as e:
            log.warning("Page %d failed: %s", i, e)

        if len(batch) >= batch_size:
            flush()

        if on_progress and (i % 5 == 0 or i == total_pages - 1):
            on_progress(phase="parsing", current_page=i + 1,
                        total_pages=total_pages,
                        imported_so_far=total_imported + len(batch))

    flush()

    if total_imported == 0:
        log.warning("No records extracted — check PDF format!")
        return 0

    set_meta("last_updated", date.today().strftime("%Y-%m-%d"))
    log.info("Done in %.1fs — %d records imported",
             time.time() - t_start, total_imported)
    return total_imported


# ══════════════════════════════════════════════════════════════════
#   PDF SOURCES
# ══════════════════════════════════════════════════════════════════
def load_pdf_sources() -> dict:
    """Load PDF sources sorted by their `order` field.

    Backwards-compatible: legacy entries without `order` get one assigned
    from their position in the file, and string-only entries (oldest format)
    are normalised to {url, district, order}.
    """
    if PDF_SOURCES_FILE.exists():
        try:
            raw = json.loads(PDF_SOURCES_FILE.read_text(encoding="utf-8"))
            normalised = {}
            for idx, (name, val) in enumerate(raw.items()):
                if isinstance(val, str):
                    entry = {"url": val, "district": "", "order": idx}
                else:
                    entry = dict(val)
                    entry.setdefault("district", "")
                    entry.setdefault("order", idx)
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


def _temp_pdf_path(office: str) -> Path:
    TEMP_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[^\w]', '_', office.lower())
    return TEMP_DIR / f"{safe}.pdf"


def _delete_temp_pdf(path: Path):
    try:
        if path.exists():
            path.unlink()
            log.info("Deleted temp PDF: %s", path.name)
    except OSError as e:
        log.warning("Could not delete temp PDF %s: %s", path.name, e)


def _download_pdf_to_file(url: str, dest: Path, headers: dict,
                          chunk_size: int = 65536) -> tuple[bool, int, str]:
    """Stream a PDF to disk in chunks. Returns (ok, bytes_written, error_msg).

    Validates the %PDF header before writing the body, writes to a .part
    file and atomically renames on success so a partial download never
    becomes a "valid" cached PDF.
    """
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
def verify_recaptcha(token: str, remote_ip: str = "") -> tuple[bool, float, str]:
    """Verify a reCAPTCHA v3 token with Google.

    Returns (ok, score, error_code). When RECAPTCHA_SECRET_KEY is unset,
    the gate is disabled — we always return ok=True, score=1.0.
    """
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
    static_folder=BASE_DIR / "static",
    static_url_path="/static",
)
app.secret_key = SECRET_KEY
CORS(app)


@app.before_request
def _ensure_db():
    init_db()


# ══════════════════════════════════════════════════════════════════
#   PWA — Service Worker & Manifest
#   These MUST be served from root (/) — not /static/ — for PWA
#   to work correctly in all browsers.
# ══════════════════════════════════════════════════════════════════
@app.route("/sw.js")
def service_worker():
    """Serve the service worker from root with no-cache headers."""
    response = app.send_static_file("sw.js")
    response.headers["Content-Type"] = "application/javascript"
    # SW must never be cached — browser needs the latest version always
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
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
    return render_template(
        "index.html",
        recaptcha_site_key=RECAPTCHA_SITE_KEY,
    )


@app.route("/api/check")
def api_check():
    raw = request.args.get("license", "").strip()
    if not raw:
        return jsonify({"error": "License number required"}), 400

    # CAPTCHA gate (no-op when RECAPTCHA_SECRET_KEY is unset).
    token = (
        request.headers.get("X-Captcha-Token", "").strip()
        or request.args.get("captcha", "").strip()
    )
    remote_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )
    ok, score, err = verify_recaptcha(token, remote_ip=remote_ip)
    if not ok:
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO search_log "
                    "(query, found, license_no, name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        raw[:128], 0, "[blocked]",
                        f"captcha:{err}:{score:.2f}"[:128],
                        datetime.utcnow().isoformat() + "Z",
                    ),
                )
        except Exception:
            pass
        return jsonify({
            "found": False,
            "error": "Verification failed. Please refresh and try again.",
            "code":  "captcha",
        }), 403

    license_no = raw.upper().replace(" ", "")
    record = find_license(license_no)

    # Log the public search so admins can see what users are looking up.
    # Wrapped in try/except: a logging failure must never break /api/check.
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO search_log "
                "(query, found, license_no, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    raw[:128],
                    1 if record else 0,
                    (record.get("license_no") if record else license_no)[:64],
                    (record.get("name") if record else "")[:128],
                    datetime.utcnow().isoformat() + "Z",
                ),
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
    rows = get_office_breakdown()
    total = sum(r["count"] for r in rows)
    return jsonify({"offices": rows, "total": total})


@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"})


# ══════════════════════════════════════════════════════════════════
#   DECOY — /admin always 404 so public never finds real panel
# ══════════════════════════════════════════════════════════════════
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
        session.permanent = True
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
#   ADMIN API — PDF SOURCES
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/urls", methods=["GET"])
@admin_required
def admin_get_urls():
    return jsonify({"urls": load_pdf_sources()})


@app.route("/dotm-admin/api/urls", methods=["POST"])
@admin_required
def admin_add_url():
    data = request.get_json() or {}
    name = data.get("name",     "").strip()
    url = data.get("url",      "").strip()
    district = data.get("district", "").strip()
    raw_order = data.get("order")
    if not name or not url:
        return jsonify({"ok": False, "error": "Name and URL required"}), 400
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    sources = load_pdf_sources()

    # Resolve target slot. Blank/missing → append at end.
    # Input is 1-based; internal order is 0-based.
    desired_slot: int | None = None
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
        # Shift everyone at or after the requested slot down by 1 to make room.
        for s in sources.values():
            cur = int(s.get("order", 0))
            if cur >= desired_slot:
                s["order"] = cur + 1
        new_order = desired_slot

    sources[name] = {"url": url, "district": district, "order": new_order}
    save_pdf_sources(sources)
    log.info("Added source: %s → %s (district: %s, order: %d)",
             name, url[:60], district, new_order)
    return jsonify({"ok": True, "order": new_order + 1})


@app.route("/dotm-admin/api/urls/reorder", methods=["POST"])
@admin_required
def admin_reorder_urls():
    data = request.get_json() or {}
    names = data.get("names")
    if not isinstance(names, list):
        return jsonify({"ok": False, "error": "names must be a list"}), 400

    sources = load_pdf_sources()
    seen = set()
    next_order = 0
    for name in names:
        if name in sources and name not in seen:
            sources[name]["order"] = next_order
            seen.add(name)
            next_order += 1
    # Append any sources the client didn't list (e.g. stale tab) at the end,
    # preserving their relative order.
    for name in sources:
        if name not in seen:
            sources[name]["order"] = next_order
            next_order += 1
    save_pdf_sources(sources)
    return jsonify({"ok": True, "order": list(load_pdf_sources().keys())})


@app.route("/dotm-admin/api/urls/<name>", methods=["DELETE"])
@admin_required
def admin_del_url(name=None):
    sources = load_pdf_sources()
    if name not in sources:
        return jsonify({"ok": False, "error": "Not found"}), 404
    _delete_temp_pdf(_temp_pdf_path(name))
    deleted_records = 0
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM licenses WHERE office = ?", (name,))
            deleted_records = cur.rowcount
    except Exception as e:
        log.error("Failed to delete DB records for %s: %s", name, e)
    del sources[name]
    save_pdf_sources(sources)
    log.info("Removed source: %s (%d records deleted)", name, deleted_records)
    return jsonify({"ok": True, "records_deleted": deleted_records})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SYNC (background job)
# ══════════════════════════════════════════════════════════════════
# A single sync job runs at a time in a daemon thread; the HTTP request
# returns immediately so the proxy never times out and the worker never
# OOMs holding a 600-page PDF in memory. The admin UI polls
# /dotm-admin/api/sync/status for progress and log lines.
SYNC_LOCK = threading.Lock()
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
            url = entry["url"] if isinstance(entry, dict) else entry
            district = entry.get("district", "") if isinstance(
                entry, dict) else ""
            tmp_pdf = _temp_pdf_path(office)

            _job_set(current_office=office, office_index=idx,
                     current_phase="checking",
                     current_page=0, total_pages=0)

            try:
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM licenses WHERE office = ?",
                        (office,)
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
        snap = dict(SYNC_STATE)
        snap["log"] = list(snap.get("log") or [])
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
#   ADMIN API — USER SEARCH LOG
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/search-log")
@admin_required
def admin_search_log():
    try:
        limit = max(1, min(int(request.args.get("limit", 200) or 200), 1000))
    except ValueError:
        limit = 200
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        offset = 0
    q = request.args.get("q", "").strip()

    with get_conn() as conn:
        if q:
            pattern = f"%{q}%"
            rows = conn.execute("""
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
            rows = conn.execute("""
                SELECT id, query, found, license_no, name, created_at
                FROM search_log
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM search_log").fetchone()[0]
    return jsonify({"rows": [dict(r) for r in rows], "total": total})


@app.route("/dotm-admin/api/search-log/clear", methods=["POST"])
@admin_required
def admin_clear_search_log():
    try:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM search_log")
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
        output.truncate(0)
        output.seek(0)
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
                output.truncate(0)
                output.seek(0)
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


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error"}), 500


# ══════════════════════════════════════════════════════════════════
#   ENTRY POINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    start_keepalive()

    port = int(os.environ.get("PORT",  5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    log.info("Server  →  http://0.0.0.0:%d", port)
    log.info("Admin   →  http://localhost:%d/dotm-admin", port)
    log.info("Login   →  %s / %s", ADMIN_USERNAME, ADMIN_PASSWORD)
    app.run(host="0.0.0.0", port=port, debug=debug)
